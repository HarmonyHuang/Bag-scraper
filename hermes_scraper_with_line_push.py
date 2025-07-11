import os
import sys
import time
import json
import hashlib
import requests
import pandas as pd
import yagmail
import gspread
import tempfile
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from google.oauth2.service_account import Credentials

# 環境變數
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_IDS = os.getenv("LINE_USER_IDS", os.getenv("LINE_USER_ID", "")).split(",")
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO = os.getenv("GMAIL_TO", "")
GSHEET_ID = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")

hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

def make_item_hash(name, color, price):
    raw = f"{(name or '').strip()}|{(color or '').strip()}|{(price or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def send_line_multicast_message(user_ids, text):
    url = "https://api.line.me/v2/bot/message/multicast"
    headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"to": [uid for uid in user_ids if uid], "messages": [{"type": "text", "text": text}]}
    r = requests.post(url, headers=headers, json=payload)
    print("LINE Multicast:", r.status_code, r.text)


def send_gmail(subject, body):
    yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
    yag.send([GMAIL_TO, "queeniechu.qc@gmail.com"], subject, body)


def get_gsheet_client():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"],
    )
    return gspread.authorize(creds)


def read_last_seen_from_gsheet():
    try:
        ws = get_gsheet_client().open_by_key(GSHEET_ID).worksheet("Sheet1")
        return {row.get('hash') for row in ws.get_all_records() if row.get('hash')}
    except Exception as e:
        print("GS read error:", e)
        return set()


def write_current_seen_to_gsheet(df):
    try:
        ws = get_gsheet_client().open_by_key(GSHEET_ID).worksheet("Sheet1")
        all_values = [df.columns.tolist()] + df.values.tolist()
        ws.clear()
        ws.update("A1", all_values)
    except Exception as e:
        print("GS write error:", e)
        df.to_json("backup_seen_items.json", force_ascii=False, indent=2)
        print("已備份至 backup_seen_items.json")


def main():
    hermes_data = []

    # 使用獨立臨時 user-data-dir 解決重複使用同一目錄問題
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument(f"--user-data-dir={tempfile.mkdtemp()}")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options
    )

    for cname, url in hermes_urls:
        driver.get(url)
        time.sleep(5)
        items = driver.find_elements(By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item")
        for item in items:
            try:
                raw_href = item.find_element(By.CSS_SELECTOR, ".product-item-name").get_attribute("href")
                link = ("https://www.hermes.com" + raw_href) if raw_href.startswith("/") else raw_href
                name = item.find_element(By.CSS_SELECTOR, ".product-item-name span").text.strip()
                color = item.find_element(By.CSS_SELECTOR, ".product-item-colors").text.strip().replace("顏色:", "").strip()
            except:
                name = link = color = ""
            try:
                price = item.find_element(By.CSS_SELECTOR, ".price").text.strip()
            except:
                price = ""
            try:
                raw_src = item.find_element(By.CSS_SELECTOR, "img").get_attribute("src")
                img = ("https:" + raw_src) if raw_src.startswith("//") else raw_src
            except:
                img = ""
            hermes_data.append({
                "source": f"Hermès官網 {cname}",
                "name": name,
                "color": color,
                "price": price,
                "link": link,
                "img": img,
            })

    driver.quit()

    df = pd.DataFrame(hermes_data)
    df["hash"] = df.apply(lambda r: make_item_hash(r["name"], r["color"], r["price"]), axis=1)

    last_seen = read_last_seen_from_gsheet()
    notify_list = []

    for _, row in df.iterrows():
        if row["hash"] not in last_seen:
            notify_list.append(f"[{row['source']}]\n{row['name']} {row['color']} {row['price']}\n{row['link']}")
            last_seen.add(row["hash"])

    if not notify_list:
        print("✅ 無新品，跳過通知。")
        return

    msg = "\n\n".join(notify_list)

    if CHANNEL_ACCESS_TOKEN and LINE_USER_IDS:
        send_line_multicast_message(LINE_USER_IDS, msg)
    if GMAIL_USER:
        send_gmail("Hermès 新上架/變價通知", msg)

    write_current_seen_to_gsheet(df)
    print("✅ Hermès通知完成且已寫入Google Sheets。")


if __name__ == "__main__":
    main()
