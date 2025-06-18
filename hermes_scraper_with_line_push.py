import os
import sys
import time
import json
import hashlib
import requests
import pandas as pd
import yagmail
import gspread
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from google.oauth2.service_account import Credentials

# ==== 環境變數 (GitHub Secrets 或本地 Export) ====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GMAIL_USER           = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD   = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO             = os.getenv("GMAIL_TO", "")
GSHEET_ID            = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON    = os.getenv("GOOGLE_CREDS_JSON", "")

hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件",     "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

def make_item_hash(name, color, price):
    name = name or ""
    color = color or ""
    price = price or ""
    raw = f"{name.strip()}|{color.strip()}|{price.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def send_line_broadcast_message(text):
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    MAX_LEN = 4900

    def chunk_text(long_text, chunk_size=MAX_LEN):
        return [long_text[i:i+chunk_size] for i in range(0, len(long_text), chunk_size)]

    if len(text) <= MAX_LEN:
        payload = {"messages": [{"type": "text", "text": text}]}
        r = requests.post(url, headers=headers, json=payload)
        print(f"LINE Broadcast 回應: {r.status_code} {r.text}")
        return [r.status_code]
    else:
        status_codes = []
        for part in chunk_text(text):
            payload = {"messages": [{"type": "text", "text": part}]}
            r = requests.post(url, headers=headers, json=payload)
            print(f"LINE Broadcast 拆段回應: {r.status_code} {r.text}")
            status_codes.append(r.status_code)
            time.sleep(0.5)
        return status_codes

def send_gmail(subject, body):
    yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
    yag.send(GMAIL_TO, subject, body)

def get_gsheet_client():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def read_last_seen_from_gsheet():
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")
        data = ws.get_all_records()
        return {row.get('hash', '') for row in data if row.get('hash')}
    except Exception as e:
        print("GS read failed, fallback to empty set:", e)
        return set()

def write_current_seen_to_gsheet(df):
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")
        all_values = [df.columns.tolist()] + df.values.tolist()
        ws.clear()
        cell_range = f"A1:G{len(all_values)}"
        ws.update(values=all_values, range_name=cell_range)
    except Exception as e:
        print("寫入 Google Sheets 失敗:", e)
        # 備份到本地 JSON
        df.to_json("backup_seen_items.json", orient="records", force_ascii=False, indent=2)
        print("已備份至 backup_seen_items.json")
    finally:
        print("【Debug結束】write_current_seen_to_gsheet 執行到最後")

def main():
    hermes_data = []
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")

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
                link = "https://www.hermes.com" + raw_href if raw_href.startswith("/") else raw_href
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
                img = "https:" + raw_src if raw_src.startswith("//") else raw_src
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
    df = pd.DataFrame(hermes_data, columns=["source", "name", "color", "price", "link", "img"])
    df["hash"] = df.apply(lambda row: make_item_hash(row["name"], row["color"], row["price"]), axis=1)

    last_seen = read_last_seen_from_gsheet()
    notify_list = []
    seen_this_time = set()

    for _, row in df.iterrows():
        h = row["hash"]
        if h not in last_seen:
            notify_list.append(f"[{row['source']}]\n{row['name']} {row['color']} {row['price']}\n{row['link']}")
            seen_this_time.add(h)

    if not notify_list:
        print("✅ 無新品，跳過通知。")
        return

    notify_msg = "\n\n".join(notify_list)

    if CHANNEL_ACCESS_TOKEN:
        send_line_broadcast_message(notify_msg)

    if GMAIL_USER:
        send_gmail("Hermès 新上架／變價通知", notify_msg)

    write_current_seen_to_gsheet(df)
    print("✅ Hermès 通知完成並寫入 Google Sheets。")

if __name__ == "__main__":
    main()
