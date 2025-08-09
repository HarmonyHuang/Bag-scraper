import os
import time
import re
import unicodedata
import json
from datetime import datetime
import requests

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

import pandas as pd
import yagmail
import gspread
from google.oauth2.service_account import Credentials

# ==== 環境變數 (GitHub Secrets 或本地 Export) ====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GMAIL_USER            = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD    = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO              = os.getenv("GMAIL_TO", "")
GSHEET_ID             = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON     = os.getenv("GOOGLE_CREDS_JSON", "")

# ===== Hermès 官方網站 要爬的兩個分類 =====
hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件",     "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

# ====== LINE Broadcast (推送給所有追蹤者)，並自動拆段超長文字 ======
def send_line_broadcast_message(text: str):
    """Broadcast 方式推播給所有追蹤者；自動拆段避免 5,000 bytes 上限。"""
    if not CHANNEL_ACCESS_TOKEN:
        print("未設定 LINE_CHANNEL_ACCESS_TOKEN，略過 LINE 推播。")
        return

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    MAX_LEN = 4900
    def chunk_text(s: str, size: int = MAX_LEN):
        return [s[i:i+size] for i in range(0, len(s), size)]

    parts = chunk_text(text) if len(text) > MAX_LEN else [text]
    for idx, part in enumerate(parts, 1):
        payload = {"messages": [{"type": "text", "text": part}]}
        r = requests.post(url, headers=headers, json=payload)
        print(f"LINE Broadcast({idx}/{len(parts)}): {r.status_code} {r.text}")
        time.sleep(0.4)

# ====== Gmail 通知 ======
def send_gmail(subject: str, body: str):
    if not GMAIL_USER:
        print("未設定 GMAIL_USER，略過 Email 通知。")
        return
    yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
    yag.send(GMAIL_TO, subject, body)

# ===== Google Sheets 客戶端與讀寫 ======
def get_gsheet_client():
    """從環境變數 GOOGLE_CREDS_JSON 讀取 Service Account JSON，並回傳 gspread client。"""
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client


def write_current_seen_to_gsheet(df: pd.DataFrame):
    """將 DataFrame 覆寫寫回 Sheet1（快照）。"""
    client = get_gsheet_client()
    sh = client.open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet("Sheet1")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Sheet1", rows="1", cols="6")

    all_values = [df.columns.tolist()] + df.values.tolist()
    ws.clear()
    cell_range = f"A1:F{len(all_values)}"
    ws.update(values=all_values, range_name=cell_range)

# ==== 防重複工具 ====
def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("顏色:", "").replace("顏色：", "")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def normalize_price(p: str) -> str:
    if not p:
        return ""
    return "".join(ch for ch in p if ch.isdigit())  # 只留數字


def get_seen_set():
    """讀取/建立 Google Sheet 的 Seen 工作表，回傳已推播 key 的集合。"""
    client = get_gsheet_client()
    sh = client.open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet("Seen")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Seen", rows="1", cols="2")
        ws.update(values=[["key", "first_seen_at"]], range_name="A1:B1")
    rows = ws.get_all_values()[1:]
    return set(r[0] for r in rows if r and r[0])


def append_seen(new_keys: set) -> bool:
    """將本輪要推播的 key 追加到 Seen；成功才回 True（避免寫失敗造成下輪重複）。"""
    if not new_keys:
        return True
    client = get_gsheet_client()
    sh = client.open_by_key(GSHEET_ID)
    ws = sh.worksheet("Seen")
    now = datetime.now().isoformat(timespec="seconds")
    values = [[k, now] for k in sorted(new_keys)]
    try:
        ws.append_rows(values)
        return True
    except Exception as e:
        print("append_seen 失敗：", e)
        return False

# ===== 主流程：爬取 Hermès 官網 + 去重 + 通知 =====
def main():
    hermes_data = []

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    # 若環境指定了 Chrome 路徑
    if os.getenv("GOOGLE_CHROME_BIN"):
        chrome_options.binary_location = os.getenv("GOOGLE_CHROME_BIN")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options,
    )

    try:
        for cname, url in hermes_urls:
            print(f"抓取 Hermès 分類: {cname} -> {url}")
            driver.get(url)
            time.sleep(5)  # 簡單等待：若不穩可改 WebDriverWait

            items = driver.find_elements(By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item")
            print(f"► 抓到 {len(items)} 件：{cname}")

            for item in items:
                try:
                    raw_href = item.find_element(By.CSS_SELECTOR, ".product-item-name").get_attribute("href")
                    link = raw_href if not raw_href.startswith("/") else f"https://www.hermes.com{raw_href}"
                except Exception:
                    link = ""

                try:
                    name = item.find_element(By.CSS_SELECTOR, ".product-item-name span").text.strip()
                except Exception:
                    name = ""

                try:
                    color = (
                        item.find_element(By.CSS_SELECTOR, ".product-item-colors").text.strip()
                    )
                except Exception:
                    color = ""

                try:
                    price = item.find_element(By.CSS_SELECTOR, ".price").text.strip()
                except Exception:
                    price = ""

                try:
                    raw_src = item.find_element(By.CSS_SELECTOR, "img").get_attribute("src")
                    img = raw_src if not raw_src.startswith("//") else f"https:{raw_src}"
                except Exception:
                    img = ""

                hermes_data.append(
                    {
                        "source": f"Hermès官網 {cname}",
                        "name": name,
                        "color": color,
                        "price": price,
                        "link": link,
                        "img": img,
                    }
                )
    finally:
        driver.quit()

    df = pd.DataFrame(hermes_data, columns=["source", "name", "color", "price", "link", "img"])

    # --- 防重複：用 Seen 記憶 + 單輪去重 ---
    seen_persistent = get_seen_set()  # 永久記憶（已推播）
    notify_list = []
    new_keys = set()
    seen_in_run = set()  # 單輪去重

    for _, row in df.iterrows():
        name_n = normalize_text(row["name"])
        color_n = normalize_text(row.get("color", ""))
        price_n = normalize_price(row["price"])
        key = f"{name_n}|{color_n}|{price_n}"

        if key in seen_in_run:
            continue
        seen_in_run.add(key)

        if key not in seen_persistent:
            notify_list.append(
                f"[{row['source']}]\n{row['name']} {row.get('color','')} {row['price']}\n{row['link']}"
            )
            new_keys.add(key)

    # 先更新快照
    write_current_seen_to_gsheet(df)

    if not notify_list:
        print("本次無新增或變價商品，跳過通知。")
        return

    # 寫入 Seen 成功才推播
    if append_seen(new_keys):
        notify_msg = "\n\n".join(notify_list)
        send_line_broadcast_message(notify_msg)
        send_gmail("Hermès 新上架／變價通知", notify_msg)
        print("通知完成。")
    else:
        print("追加 Seen 失敗，為避免重複推播，本輪不發送通知。")


if __name__ == "__main__":
    # GitHub Actions 以排程單次執行，不要 while True
    main()
