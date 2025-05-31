import os
import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
import pandas as pd
import yagmail
import gspread
import json
from google.oauth2.service_account import Credentials

# ==== 環境變數 (請在 GitHub Secrets 或本地環境設定) ====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID            = os.getenv("LINE_USER_ID", "")
GMAIL_USER              = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD      = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO                = os.getenv("GMAIL_TO", "")
GSHEET_ID               = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON       = os.getenv("GOOGLE_CREDS_JSON", "")

# ===== Hermès 官網要爬的兩個分類 =====
hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件",     "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

# ===== 2nd STREET 要爬的三個品牌 =====
second_urls = [
    ("HERMES",         "https://store.2ndstreet.com.tw/v2/Search?q=HERMES&shopId=41320&order=Newest"),
    ("CHANEL",         "https://store.2ndstreet.com.tw/v2/Search?q=CHANEL&shopId=41320&order=Newest"),
    ("Christian Dior", "https://store.2ndstreet.com.tw/v2/Search?q=Christian+Dior&shopId=41320&order=Newest"),
]

# ===== LINE 推播：支援長訊息自動拆段 =====
def send_line_bot_message(user_id, text):
    """
    如果 text 長度 <= 5000，直接呼叫一次 push API。
    如果 text 長度 > 5000，則自動拆成多段 (每段 <= 4900 字) 逐段發送。
    """
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    # LINE single text 的最大長度為 5000 bytes (約 5000 個中文字)
    MAX_LEN = 4900  # 稍微預留些空間

    # 幫忙把長文字切段：
    def chunk_text(long_text, chunk_size=MAX_LEN):
        chunks = []
        start = 0
        while start < len(long_text):
            chunks.append(long_text[start:start+chunk_size])
            start += chunk_size
        return chunks

    # 如果這段文字本身就超過 MAX_LEN，拆成多段
    if len(text) <= MAX_LEN:
        payload = {
            "to": user_id,
            "messages": [{"type": "text", "text": text}]
        }
        r = requests.post(url, headers=headers, json=payload)
        print(f"LINE Messaging API 回應: {r.status_code} {r.text}")
        return r.status_code
    else:
        # 拆段逐段發
        status_codes = []
        chunks = chunk_text(text, MAX_LEN)
        for part in chunks:
            payload = {
                "to": user_id,
                "messages": [{"type": "text", "text": part}]
            }
            r = requests.post(url, headers=headers, json=payload)
            print(f"LINE Messaging API 回應 (拆段): {r.status_code} {r.text}")
            status_codes.append(r.status_code)
            time.sleep(0.5)  # 稍微休息，避免瞬間送太多
        return status_codes

# ===== Gmail 寄信 =====
def send_gmail(subject, body):
    yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
    yag.send(GMAIL_TO, subject, body)

# ===== Google Sheets 操作 =====
def get_gsheet_client():
    print("進入 get_gsheet_client()")
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    print("Google Sheets 驗證成功")
    return client

def read_last_seen_from_gsheet():
    print("進入 read_last_seen_from_gsheet()")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")
        data = ws.get_all_records()
        print(f"[讀取] Google Sheet 共 {len(data)} 筆")
        last_set = set()
        for row in data:
            key = f"{row['name']}|{row['price']}"
            last_set.add(key)
        return last_set
    except Exception as e:
        print("GS read failed, fallback to empty set:", e)
        return set()

def write_current_seen_to_gsheet(df):
    """
    直接把整個 DataFrame 一次性更新到 Google Sheets：
    先把第一列欄位名稱與所有列值放進一個二維 list，
    再用 ws.update(range, values) 一次全部寫入，避免大量 append_row 調用造成配額不足。
    """
    print("==== 準備寫入 Google Sheets ====")
    print(df.head())
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")
        # 把 DataFrame 轉成二維 list
        all_values = [df.columns.tolist()] + df.values.tolist()
        # 清空之後一次性更新
        ws.clear()
        # 一次把整塊範圍貼上去 (A1 開始)
        cell_range = f"A1:F{len(all_values)}"
        ws.update(cell_range, all_values)
        print("==== 已經寫入 Google Sheets ====")
    except Exception as e:
        print("寫入 Google Sheets 失敗:", e)
    finally:
        print("【Debug結束】write_current_seen_to_gsheet 執行到最後")

# ===== 抓 Hermès 官網：兩個分類 =====
hermes_data = []
chrome_options = Options()
chrome_options.add_argument('--headless')
chrome_options.add_argument('--no-sandbox')
chrome_options.add_argument('--disable-dev-shm-usage')

driver = webdriver.Chrome(
    service=Service(ChromeDriverManager().install()),
    options=chrome_options
)

for cname, url in hermes_urls:
    print(f"抓取 Hermès 分類: {cname}")
    driver.get(url)
    time.sleep(5)  # 等待頁面渲染完成
    items = driver.find_elements(By.CSS_SELECTOR, "div.product-grid-list-item")
    print(f"► 本次共抓到 {len(items)} 件 Hermès 「{cname}」")
    for item in items:
        try:
            name  = item.find_element(By.CSS_SELECTOR, ".product-item-name").text.strip()
            link  = item.find_element(By.CSS_SELECTOR, ".product-item-name").get_attribute("href")
            color = item.find_element(By.CSS_SELECTOR, ".product-item-colors").text.strip().replace("顏色:", "").strip()
        except:
            name = link = color = ""
        try:
            price = item.find_element(By.CSS_SELECTOR, ".price").text.strip()
        except:
            price = ""
        try:
            img = item.find_element(By.CSS_SELECTOR, "img").get_attribute("src")
        except:
            img = ""
        hermes_data.append({
            "source": f"Hermès官網 {cname}",
            "name":   name,
            "color":  color,
            "price":  price,
            "link":   link,
            "img":    img
        })

driver.quit()

# ===== 抓 2nd STREET (Selenium 動態滾動) =====
second_data = []

for brand, url in second_urls:
    print(f"抓取 2nd STREET: {brand} (Selenium 版動態載入)")
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options
    )
    driver.get(url)
    time.sleep(3)  # 等待 React 初步載入

    # 動態滾動到最底部，讓所有商品都載入
    SCROLL_PAUSE_SEC = 2
    last_height = driver.execute_script("return document.body.scrollHeight")
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(SCROLL_PAUSE_SEC)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    # 取得完整渲染後的 HTML
    page_source = driver.page_source
    soup = BeautifulSoup(page_source, "html.parser")

    # 根據實際檢查結果，最外層商品卡片是 <li class="column-grid-container__column">
    cards = soup.select("li.column-grid-container__column")
    print(f"► 本次共找到 {len(cards)} 張 2nd STREET「{brand}」商品卡片")

    for card in cards:
        try:
            # (1) 連結： <a href="…">
            a_tag = card.select_one("a")
            raw_href = a_tag["href"] if (a_tag and a_tag.has_attr("href")) else ""
            link = raw_href if raw_href.startswith("http") else f"https://store.2ndstreet.com.tw{raw_href}"

            # (2) 圖片與名稱：<img class="product-card__vertical__media …" alt="…">
            img_tag = card.select_one("img.product-card__vertical__media")
            if img_tag and img_tag.has_attr("src"):
                raw_src = img_tag["src"]
                if raw_src.startswith("//"):
                    img = f"https:{raw_src}"
                elif raw_src.startswith("http"):
                    img = raw_src
                else:
                    img = f"https://{raw_src}"
            else:
                img = ""
            name = img_tag["alt"].strip() if (img_tag and img_tag.has_attr("alt")) else ""

            # (3) 價格：<div class="sc-lgQHWK eQJqfn">NT$ xx,xxx</div>
            price_tag = card.select_one("div.sc-lgQHWK.eQJqfn")
            price = price_tag.text.strip() if price_tag else ""

            # 2nd STREET 通常不顯示「color」
            color = ""

        except Exception:
            name = link = color = price = img = ""

        second_data.append({
            "source": f"2nd STREET {brand}",
            "name":   name,
            "color":  color,
            "price":  price,
            "link":   link,
            "img":    img
        })

    driver.quit()

# ===== 合併所有商品，準備比對新品／變價 =====
data = hermes_data + second_data
df = pd.DataFrame(data)

# ===== 讀取 Google Sheets 上一次的 name|price set =====
last_set = read_last_seen_from_gsheet()

# ===== 比對：如果 name|price 不在 last_set，就把它當作「新品/變價」加入通知 =====
notify_list = []
for _, row in df.iterrows():
    key = f"{row['name']}|{row['price']}"
    if key not in last_set:
        msg = f"[{row['source']}]\n{row['name']} {row.get('color','')} {row['price']}\n{row['link']}"
        notify_list.append(msg)

# 如果沒有任何新品/變價，就不發通知
if notify_list:
    notify_msg = "\n\n".join(notify_list)

    # ===== 發送 LINE 通知（如過長會自動拆段） =====
    if CHANNEL_ACCESS_TOKEN and LINE_USER_ID:
        print("發送 LINE 訊息")
        send_line_bot_message(LINE_USER_ID, notify_msg)

    # ===== 發送 GMAIL 通知 =====
    if GMAIL_USER:
        print("發送 GMAIL")
        send_gmail("Hermès/2nd STREET 新上架商品", notify_msg)

else:
    print("本次無新增或變價商品，跳過通知。")

# ===== 最後把本次所有商品寫回 Google Sheets （一次性更新） =====
write_current_seen_to_gsheet(df)

print("只推播新品/變價商品（Google Sheets 記憶版）完成！")
