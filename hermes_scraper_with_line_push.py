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

# ==== Github secrets 參數 ====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO = os.getenv("GMAIL_TO", "")
GSHEET_ID = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")

hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]
second_urls = [
    ("HERMES", "https://store.2ndstreet.com.tw/v2/Search?q=HERMES&shopId=41320&order=Newest"),
    ("CHANEL", "https://store.2ndstreet.com.tw/v2/Search?q=CHANEL&shopId=41320&order=Newest"),
    ("Christian Dior", "https://store.2ndstreet.com.tw/v2/Search?q=Christian+Dior&shopId=41320&order=Newest"),
]

# ===== LINE Messaging API (BOT) =====
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

# ===== Google Sheets Utility =====
def get_gsheet_client():
    print("進入 get_gsheet_client()")
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    print("Google Sheets 驗證成功")
    return client

def read_last_seen_from_gsheet():
    print("進入 read_last_seen_from_gsheet()")
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

def write_current_seen_to_gsheet(df):
    print("==== 準備寫入 Google Sheets ====")
    print(df.head())
    try:
        client = get_gsheet_client()
        print('client get success')
        sh = client.open_by_key(GSHEET_ID)
        print('open sheet success')
        ws = sh.worksheet("Sheet1")
        print('get worksheet success')
        ws.clear()
        print('sheet clear success')
        ws.append_row(df.columns.tolist())
        print('append col success')
        for row in df.itertuples(index=False):
            print('正在寫入 row:', row)
            ws.append_row(list(row))
        print("==== 已經寫入 Google Sheets ====")
    except Exception as e:
        print("寫入 Google Sheets 失敗:", e)
    finally:
        print("【Debug結束】write_current_seen_to_gsheet 執行到最後")

# ===== Hermès 官網多分類 =====
hermes_data = []
options = webdriver.ChromeOptions()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
for cname, url in hermes_urls:
    print(f"抓取 Hermès 分類: {cname}")
    driver.get(url)
    time.sleep(5)
    items = driver.find_elements(By.CSS_SELECTOR, "div.product-grid-list-item")
    for item in items:
        try:
            name = item.find_element(By.CSS_SELECTOR, ".product-item-name").text.strip()
            link = item.find_element(By.CSS_SELECTOR, ".product-item-name").get_attribute('href')
            color = item.find_element(By.CSS_SELECTOR, ".product-item-colors").text.strip().replace("顏色:", "").strip()
        except Exception:
            name = link = color = ""
        try:
            price = item.find_element(By.CSS_SELECTOR, ".price").text.strip()
        except Exception:
            price = ""
        try:
            img = item.find_element(By.CSS_SELECTOR, "img").get_attribute("src")
        except Exception:
            img = ""
        hermes_data.append({
            "source": f"Hermès官網 {cname}",
            "name": name,
            "color": color,
            "price": price,
            "link": link,
            "img": img
        })
driver.quit()

# ===== 2nd STREET 多品牌 =====
second_data = []
for brand, url in second_urls:
    print(f"抓取 2nd STREET: {brand}")
    res = requests.get(url)
    soup = BeautifulSoup(res.text, 'html.parser')
    items = soup.select('div.p-list__item')
    for item in items:
        try:
            name = item.select_one('h2.p-list__item__name').text.strip()
            raw_link = item.select_one('a.p-list__item__inner')['href']
            link = raw_link if raw_link.startswith('http') else f"https://store.2ndstreet.com.tw{ raw_link }"
            price = item.select_one('div.p-list__item__price').text.strip().replace('\n', '')
            raw_img = item.select_one('img')['src']
            # 如果是協議相對路徑，就補 https:
            if raw_img.startswith('//'):
                img = f"https:{ raw_img }"
            elif raw_img.startswith('http'):
                img = raw_img
            else:
                img = f"https://store.2ndstreet.com.tw{ raw_img }"
        except Exception:
            name = link = price = img = ""
        second_data.append({
            "source": f"2nd STREET {brand}",
            "name": name,
            "color": "",
            "price": price,
            "link": link,
            "img": img
        })

# ===== 合併資料 =====
data = hermes_data + second_data
df = pd.DataFrame(data)

# ===== 判斷新品/價格異動 only，雲端保存 last_seen =====
try:
    last_set = read_last_seen_from_gsheet()
except Exception as e:
    print("GS read failed, fallback to empty set:", e)
    last_set = set()

notify_list = []
for _, row in df.iterrows():
    key = f"{row['name']}|{row['price']}"
    if key not in last_set:
        msg = f"[{row['source']}]\n{row['name']} {row.get('color', '')} {row['price']}\n{row['link']}"
        notify_list.append(msg)

notify_msg = "\n\n".join(notify_list)

if notify_msg and CHANNEL_ACCESS_TOKEN and LINE_USER_ID:
    print("發送 LINE 訊息")
    send_line_bot_message(LINE_USER_ID, notify_msg)
if notify_msg and GMAIL_USER:
    print("發送 GMAIL")
    send_gmail("Hermès/2nd STREET 新上架商品", notify_msg)

write_current_seen_to_gsheet(df)

print("只推播新品/變價商品（Google Sheets記憶版）完成！")
