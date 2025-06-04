import os
import sys
import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
import pandas as pd
import yagmail
import gspread
import json
from google.oauth2.service_account import Credentials
from fake_useragent import UserAgent
import logging

# ===== 設定日誌，方便追蹤 =====
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ==== 環境變數（需先放到 GitHub Secrets 或本機 export） ====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GMAIL_USER            = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD    = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO              = os.getenv("GMAIL_TO", "")
GSHEET_ID             = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON     = os.getenv("GOOGLE_CREDS_JSON", "")

# ===== 要抓的 Hermès 官網分類 =====
hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件",     "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

# ===== LINE Broadcast（推播給所有追蹤者），自動拆段超長文字 =====
def send_line_broadcast_message(text):
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    MAX_LEN = 4900

    def chunk_text(long_text, chunk_size=MAX_LEN):
        chunks = []
        start = 0
        while start < len(long_text):
            chunks.append(long_text[start : start + chunk_size])
            start += chunk_size
        return chunks

    if len(text) <= MAX_LEN:
        payload = {"messages": [{"type": "text", "text": text}]}
        r = requests.post(url, headers=headers, json=payload)
        logging.info(f"LINE Broadcast 回應: {r.status_code} {r.text}")
        return [r.status_code]
    else:
        status_codes = []
        for part in chunk_text(text):
            payload = {"messages": [{"type": "text", "text": part}]}
            r = requests.post(url, headers=headers, json=payload)
            logging.info(f"LINE Broadcast 拆段回應: {r.status_code} {r.text}")
            status_codes.append(r.status_code)
            time.sleep(0.5)
        return status_codes

# ===== Gmail 通知 =====
def send_gmail(subject, body):
    try:
        yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
        yag.send(GMAIL_TO, subject, body)
        logging.info("Gmail 通知已發送")
    except Exception as e:
        logging.error(f"發送 Gmail 失敗: {e}", exc_info=True)

# ===== Google Sheets：建立 client & 讀／寫 =====
def get_gsheet_client():
    logging.info("進入 get_gsheet_client()")
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        logging.info("Google Sheets 驗證成功")
        return client
    except Exception as e:
        logging.error(f"Google Sheets 驗證失敗: {e}", exc_info=True)
        raise

def read_last_seen_from_gsheet():
    logging.info("進入 read_last_seen_from_gsheet()")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")
        data = ws.get_all_records()
        logging.info(f"[讀取] Google Sheet 共 {len(data)} 筆")
        last_set = set()
        for row in data:
            # key = name|color|price
            key = f"{row.get('name','')}|{row.get('color','')}|{row.get('price','')}"
            last_set.add(key)
        return last_set
    except Exception as e:
        logging.error(f"GS read failed: {e}", exc_info=True)
        return set()

def write_current_seen_to_gsheet(df):
    logging.info("==== 準備寫入 Google Sheets ====")
    logging.debug(f"要寫入的 DataFrame 頭幾列:\n{df.head()}")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")

        # 將 DataFrame 轉成二維陣列
        all_values = [df.columns.tolist()] + df.values.tolist()
        logging.info("正在清空 Google Sheet...")
        ws.clear()
        logging.info("Google Sheet 已清空。")

        max_row = len(all_values)
        cell_range = f"A1:F{max_row}"
        logging.info(f"正在寫入資料到 Google Sheet，範圍：{cell_range} ...")
        ws.update(values=all_values, range_name=cell_range)
        logging.info("==== 已經寫入 Google Sheets ====")

    except Exception as e:
        logging.error(f"寫入 Google Sheets 失敗: {e}", exc_info=True)
    finally:
        logging.info("【Debug結束】write_current_seen_to_gsheet 執行到最後")

def normalize_price(price_str):
    return "".join(filter(str.isdigit, price_str))

def main():
    # ------------------------------------------------------------
    # 1. 用 Selenium 爬 Hermès 官網
    # ------------------------------------------------------------
    hermes_data = []

    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")

    ua = UserAgent()
    user_agent = ua.random
    chrome_options.add_argument(f"--user-agent={user_agent}")
    logging.info(f"使用的 User-Agent：{user_agent}")

    # 禁用圖片，加快速度
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    logging.info("已禁用圖片載入")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options
    )
    driver.set_page_load_timeout(30)

    for cname, url in hermes_urls:
        logging.info(f"抓取 Hermès 分類：{cname} → {url}")
        try:
            driver.get(url)
            # 先等到商品容器出現再繼續
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item"))
            )
            # 再多等幾秒，確保 JS 完全渲染完
            time.sleep(8)
        except TimeoutException:
            logging.error(f"載入 {cname} 頁面超時，跳過這個分類")
            continue

        items = driver.find_elements(By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item")
        logging.info(f"► 本次共抓到 {len(items)} 件 Hermès 「{cname}」")

        for item in items:
            try:
                # 1) 名稱 + 連結
                name = ""
                link = ""
                try:
                    a_el = item.find_element(By.CSS_SELECTOR, ".product-item-name")
                    name = a_el.text.strip()
                    raw_href = a_el.get_attribute("href")
                    if raw_href:
                        link = raw_href if raw_href.startswith("http") else "https://www.hermes.com" + raw_href
                except NoSuchElementException:
                    # 若找不到.product-item-name，就略過這一筆
                    continue

                # 2) 顏色
                color = ""
                try:
                    color_el = item.find_element(By.CSS_SELECTOR, ".product-item-colors")
                    color = color_el.text.strip().replace("顏色:", "").strip()
                except NoSuchElementException:
                    color = ""

                # 3) 價格
                price = ""
                try:
                    price_el = item.find_element(By.CSS_SELECTOR, ".price")
                    price = price_el.text.strip()
                except NoSuchElementException:
                    price = ""

                # 4) 圖片
                img = ""
                try:
                    img_el = item.find_element(By.CSS_SELECTOR, "img")
                    raw_src = img_el.get_attribute("src")
                    if raw_src:
                        img = raw_src if raw_src.startswith("http") else "https:" + raw_src
                except NoSuchElementException:
                    img = ""

                logging.debug(f"處理商品 → name：{name}；color：{color}；price：{price}；link：{link}")

                # 只要 name 和 link 都存在，就放進去
                if name and link:
                    hermes_data.append({
                        "source": f"Hermès官網 {cname}",
                        "name":   name,
                        "color":  color,
                        "price":  price,
                        "link":   link,
                        "img":    img,
                    })

            except Exception as e:
                logging.error(f"處理單筆商品時發生例外：{e}", exc_info=True)
                continue

    driver.quit()
    logging.debug(f"爬取完畢，hermes_data 共 {len(hermes_data)} 筆 → 頭五筆：{hermes_data[:5]}")

    # ------------------------------------------------------------
    # 2. 建立 DataFrame
    # ------------------------------------------------------------
    df = pd.DataFrame(hermes_data, columns=["source","name","color","price","link","img"])

    # ------------------------------------------------------------
    # 3. 如果完全沒抓到，僅寫入「標題列」，然後結束
    # ------------------------------------------------------------
    if df.empty:
        logging.warning("!!! 本次 Selenium 並未抓到任何商品（hermes_data 為空），僅寫入標題後結束。")
        write_current_seen_to_gsheet(df)
        sys.exit(0)

    # ------------------------------------------------------------
    # 4. 讀 Google Sheets 上次已見 (name|color|price)
    # ------------------------------------------------------------
    last_set = read_last_seen_from_gsheet()

    # ------------------------------------------------------------
    # 5. 比對「新品/變價」，產生要通知的清單
    # ------------------------------------------------------------
    notify_list = []
    new_keys = set()

    for _, row in df.iterrows():
        key = f"{row['name']}|{row['color']}|{normalize_price(row['price'])}"
        if key not in last_set:
            logging.info(f"發現新品／變價 => {key}")
            notify_list.append(
                f"[{row['source']}]\n{row['name']} {row.get('color','')} {row['price']}\n{row['link']}"
            )
            new_keys.add(key)
        else:
            logging.debug(f"此筆已在上次清單，跳過：{key}")

    # 若沒有任何要通知的，就僅更新整張 Google Sheets，然後結束
    if not notify_list:
        logging.info("本次無新增或變價商品，僅更新 Google Sheets，跳過通知。")
        write_current_seen_to_gsheet(df)
        sys.exit(0)

    # ------------------------------------------------------------
    # 6. 把「本次完整結果 df」一次性寫回 Google Sheets
    # ------------------------------------------------------------
    logging.debug(f"準備寫入 Google Sheets 的 DataFrame 頭兩筆：\n{df.head(2)}")
    write_current_seen_to_gsheet(df)

    # ------------------------------------------------------------
    # 7. 合併文字後，Broadcast 給所有追蹤者
    # ------------------------------------------------------------
    notify_msg = "\n\n".join(notify_list)

    # LINE Broadcast
    if CHANNEL_ACCESS_TOKEN:
        logging.info("發送 LINE Broadcast 訊息")
        send_line_broadcast_message(notify_msg)
    else:
        logging.warning("LINE_CHANNEL_ACCESS_TOKEN 未設定，跳過 Broadcast。")

    # Gmail
    if GMAIL_USER and GMAIL_TO and GMAIL_APP_PASSWORD:
        logging.info("發送 Gmail 通知")
        send_gmail("Hermès 新上架／變價通知", notify_msg)
    elif GMAIL_USER:
        logging.warning("GMAIL_TO 或 GMAIL_APP_PASSWORD 未設定，跳過 Gmail 通知。")
    else:
        logging.warning("GMAIL_USER 未設定，跳過 Gmail 通知。")

    logging.info("只推播 Hermès 新品／變價商品（Broadcast + Google Sheets 記憶版）完成！")

if __name__ == "__main__":
    main()
