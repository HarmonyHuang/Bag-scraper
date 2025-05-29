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
from google.oauth2.service_account import Credentials

# ====== 參數 ======
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "你的_Channel_Access_Token")
LINE_USER_ID = os.getenv("LINE_USER_ID", "你的_userId")
GMAIL_USER = os.getenv("GMAIL_USER", "你的Gmail帳號")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "你的Gmail應用程式密碼")
GMAIL_TO = os.getenv("GMAIL_TO", "收件人信箱")

# 以下改成你自己的 spreadsheet_id
GSHEET_ID = os.getenv("GSHEET_ID", "你的 Google Sheets id")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "service_account.json 字串內容")

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
    import json
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client

def read_last_seen_from_gsheet():
    client = get_gsheet_client()
    sh = client.open_by_key(GSHEET_ID)
    ws = sh.worksheet("Sheet1")
    data = ws.get_all_records()
    last_set = set()
    for row in data:
        key = f"{row['name']}|{row['price']}"
        last_set.add(key)
    return last_set

def write_current_seen_to_gsheet(df):
    client = get_gsheet_client()
    sh = client.open_by_key(GSHEET_ID)
    ws = sh.worksheet("Sheet1")
    ws.clear()
    ws.append_row(df.columns.tolist())
    for row in df.itertuples(index=False):
        ws.append_row(list(row))

# ===== Hermès/2nd STREET 爬蟲略（同你原本） =====
# ...（hermes_data, second_data 等同你原本）

# ===== 合併資料 =====
data = hermes_data + second_data
df = pd.DataFrame(data)
# df.to_csv('hermes_and_2ndstreet.csv', index=False, encoding='utf-8-sig')

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

if notify_msg and "你的_Channel_Access_Token" not in CHANNEL_ACCESS_TOKEN and "你的_userId" not in LINE_USER_ID:
    send_line_bot_message(LINE_USER_ID, notify_msg)
if notify_msg and "你的Gmail帳號" not in GMAIL_USER:
    send_gmail("Hermès/2nd STREET 新上架商品", notify_msg)

# ==== 寫回雲端記憶庫 ====
try:
    write_current_seen_to_gsheet(df)
except Exception as e:
    print("GS write failed:", e)

print("只推播新品/變價商品（Google Sheets記憶版）完成！")
