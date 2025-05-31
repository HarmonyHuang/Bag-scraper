import os
import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
import pandas as pd
import yagmail
import gspread
import json
from google.oauth2.service_account import Credentials

# ==== 環境變數（請從 GitHub Secrets 或本地環境匯入） ====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID            = os.getenv("LINE_USER_ID", "")
GMAIL_USER              = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD      = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO                = os.getenv("GMAIL_TO", "")
GSHEET_ID               = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON       = os.getenv("GOOGLE_CREDS_JSON", "")

# Hermès 官網要爬的兩個分類
hermes_urls = [
    ("包包&手拿包",    "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件",        "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

# 2nd STREET 要爬的三個品牌
second_urls = [
    ("HERMES",         "https://store.2ndstreet.com.tw/v2/Search?q=HERMES&shopId=41320&order=Newest"),
    ("CHANEL",         "https://store.2ndstreet.com.tw/v2/Search?q=CHANEL&shopId=41320&order=Newest"),
    ("Christian Dior", "https://store.2ndstreet.com.tw/v2/Search?q=Christian+Dior&shopId=41320&order=Newest"),
]

# ===== 不要動這兩個函式，負責發 LINE & Gmail 通知 =====
def send_line_bot_message(user_id, text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "to": user_id,
        "messages": [{"type": "text", "text": text}]
    }
    r = requests.post(url, headers=headers, json=data)
    print(f"LINE Messaging API 回應: {r.status_code} {r.text}")
    return r.status_code

def send_gmail(subject, body):
    yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
    yag.send(GMAIL_TO, subject, body)

# ===== Google Sheets 操作輔助 =====
def get_gsheet_client():
    """
    由 GOOGLE_CREDS_JSON 建立 gspread client。
    若 GOOGLE_CREDS_JSON 為空或格式錯誤，會拋例外。
    """
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
    """
    從 Sheet1 讀取所有已經紀錄下來的 name|price 組合，
    回傳為一個 set，若讀取過程出錯則回傳空集合。
    """
    print("進入 read_last_seen_from_gsheet()")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")
        data = ws.get_all_records()
        print(f"[讀取] Google Sheet 共 {len(data)} 筆")
        last_set = set()
        for row in data:
            key = f"{ row['name'] }|{ row['price'] }"
            last_set.add(key)
        return last_set
    except Exception as e:
        print("GS read failed, fallback to empty set:", e)
        return set()

def write_current_seen_to_gsheet(df):
    """
    將當前 DataFrame (包含所爬到的所有商品) 寫回 Sheet1。
    先清空，再把表頭與每一列 append 上去，作為下一次的比對基準。
    """
    print("==== 準備寫入 Google Sheets ====")
    print(df.head())
    try:
        client = get_gsheet_client()
        print("client get success")
        sh = client.open_by_key(GSHEET_ID)
        print("open sheet success")
        ws = sh.worksheet("Sheet1")
        print("get worksheet success")
        ws.clear()
        print("sheet clear success")
        ws.append_row(df.columns.tolist())
        print("append col success")
        for row in df.itertuples(index=False):
            print("正在寫入 row:", row)
            ws.append_row(list(row))
        print("==== 已經寫入 Google Sheets ====")
    except Exception as e:
        print("寫入 Google Sheets 失敗:", e)
    finally:
        print("【Debug結束】write_current_seen_to_gsheet 執行到最後")

# ===== 抓 Hermès 官網：兩個分類 =====
hermes_data = []
options = webdriver.ChromeOptions()
options.add_argument('--headless')            # 無頭模式
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
driver = webdriver.Chrome(
    service=Service(ChromeDriverManager().install()),
    options=options
)

for cname, url in hermes_urls:
    print(f"抓取 Hermès 分類: {cname}")
    driver.get(url)
    time.sleep(5)  # 有時候網頁渲染需要多給點時間
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

# ===== 抓 2nd STREET：三個品牌（新版 CSS selector） =====
second_data = []
for brand, url in second_urls:
    print(f"抓取 2nd STREET: {brand}")
    res = requests.get(url)
    soup = BeautifulSoup(res.text, "html.parser")

    # 先抓所有最外層 <div class="product-card__vertical">
    cards = soup.select("div.product-card__vertical")
    print(f"► 本次共找到 {len(cards)} 張 2nd STREET「{brand}」商品卡片 (product-card__vertical)")

    for card in cards:
        try:
            # 1) 名稱
            name_tag = card.select_one("h3.product-card__vertical__name")
            name = name_tag.text.strip() if name_tag else ""

            # 2) 連結
            link_tag = card.select_one("a.product-card__vertical__link")
            raw_href = link_tag["href"] if (link_tag and link_tag.has_attr("href")) else ""
            # 若是相對路徑，就補上 base URL
            link = raw_href if raw_href.startswith("http") else f"https://store.2ndstreet.com.tw{raw_href}"

            # 3) 價格
            price_tag = card.select_one("p.product-card__vertical__price")
            price = price_tag.text.strip().replace("\n", "") if price_tag else ""

            # 4) 圖片
            img_tag = card.select_one("img.product-card__vertical__media")
            raw_src = img_tag["src"] if (img_tag and img_tag.has_attr("src")) else ""
            if raw_src.startswith("//"):
                img = f"https:{raw_src}"
            elif raw_src.startswith("http"):
                img = raw_src
            else:
                img = f"https://{raw_src}"

        except Exception:
            name = link = price = img = ""

        second_data.append({
            "source": f"2nd STREET {brand}",
            "name":   name,
            "color":  "",
            "price":  price,
            "link":   link,
            "img":    img
        })

# ===== 合併所有資料，準備做「新品/變價」比對 =====
data = hermes_data + second_data
df = pd.DataFrame(data)

# ===== 從 Google Sheets 讀取 last_seen (name|price) set =====
last_set = read_last_seen_from_gsheet()

# ===== 比對：如果 name|price 不在 last_set，表示是「新品或價格變動」 =====
notify_list = []
for _, row in df.iterrows():
    key = f"{row['name']}|{row['price']}"
    if key not in last_set:
        msg = f"[{row['source']}]\n{row['name']} {row.get('color','')} {row['price']}\n{row['link']}"
        notify_list.append(msg)

notify_msg = "\n\n".join(notify_list)

# ===== 發送通知 =====
if notify_msg and CHANNEL_ACCESS_TOKEN and LINE_USER_ID:
    print("發送 LINE 訊息")
    send_line_bot_message(LINE_USER_ID, notify_msg)

if notify_msg and GMAIL_USER:
    print("發送 GMAIL")
    send_gmail("Hermès/2nd STREET 新上架商品", notify_msg)

# ===== 最後把本次所有商品回寫到 Google Sheets (作為下一輪比對) =====
write_current_seen_to_gsheet(df)

print("只推播新品/變價商品（Google Sheets 記憶版）完成！")
