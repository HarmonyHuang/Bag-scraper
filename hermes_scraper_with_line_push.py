# hermes_scraper.py

import os
import sys
import time
import requests
from bs4 import BeautifulSoup
import pandas as pd
import yagmail
import gspread
import json
from google.oauth2.service_account import Credentials

# ==== 環境變數 (GitHub Secrets 或本地 export) ====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GMAIL_USER           = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD   = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO             = os.getenv("GMAIL_TO", "")
GSHEET_ID            = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON    = os.getenv("GOOGLE_CREDS_JSON", "")

# ===== Hermès 官網 要爬的兩個分類 =====
hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件",     "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

# ====== LINE Broadcast：自動拆段超長文字 ======
def send_line_broadcast_message(text):
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    MAX_LEN = 4900
    def chunk_text(s):
        return [s[i:i+MAX_LEN] for i in range(0, len(s), MAX_LEN)]
    parts = chunk_text(text) if len(text)>MAX_LEN else [text]
    for part in parts:
        payload = {"messages":[{"type":"text","text":part}]}
        r = requests.post(url, headers=headers, json=payload)
        print(f"LINE 回應: {r.status_code} {r.text}")
        time.sleep(0.3)

# ====== Gmail 通知 ======
def send_gmail(subject, body):
    yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
    yag.send(GMAIL_TO, subject, body)

# ====== Google Sheets 客戶端與讀寫 ======
def get_gsheet_client():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def read_last_seen_from_gsheet():
    try:
        client = get_gsheet_client()
        ws = client.open_by_key(GSHEET_ID).worksheet("Sheet1")
        rows = ws.get_all_records()
        seen = set(f"{r.get('name','')}|{r.get('color','')}|{r.get('price','')}" for r in rows)
        print(f"[GS] 讀取到 {len(seen)} 筆已見資料")
        return seen
    except Exception as e:
        print("讀 GS 失敗，將視為空集合:", e)
        return set()

def write_current_seen_to_gsheet(df: pd.DataFrame):
    client = get_gsheet_client()
    ws = client.open_by_key(GSHEET_ID).worksheet("Sheet1")
    all_values = [df.columns.tolist()] + df.values.tolist()
    ws.clear()
    ws.update(values=all_values, range_name=f"A1:F{len(all_values)}")
    print("[GS] 已更新所有資料")

# ====== 主流程 ======
def main():
    hermes_data = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/110.0.0.0 Safari/537.36"
        )
    }

    # 1) 抓兩個分類
    for cname, url in hermes_urls:
        print(f"爬取分類：{cname}")
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            print(f"  無法載入 {url}，跳過")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select("div.product-grid-list-item, div.product-grid-item")
        print(f"► 本次共抓到 {len(items)} 件「{cname}」")
        for it in items:
            name=color=price=link=img=""
            # 連結與名稱
            a = it.select_one(".product-item-name")
            if a:
                href = a.get("href","").strip()
                link = "https://www.hermes.com"+href if href.startswith("/") else href
                sp = a.select_one("span")
                if sp: name = sp.get_text(strip=True)
            # 顏色
            ctag = it.select_one(".product-item-colors")
            if ctag: color = ctag.get_text(strip=True).replace("顏色:","").strip()
            # 價格
            ptag = it.select_one("span.price")
            if ptag: price = ptag.get_text(strip=True)
            # 圖片
            im = it.select_one("img")
            if im:
                src = im.get("src","").strip()
                img = ("https:"+src) if src.startswith("//") else src
            if name or link:
                hermes_data.append({
                    "source": f"Hermès官網 {cname}",
                    "name":   name,
                    "color":  color,
                    "price":  price,
                    "link":   link,
                    "img":    img,
                })
        time.sleep(1)

    if not hermes_data:
        print("!!! 未抓到任何資料，請檢查 CSS selector 或網頁結構。")
        sys.exit(1)

    # 2) 組 DataFrame
    df = pd.DataFrame(hermes_data, columns=["source","name","color","price","link","img"])

    # 3) 讀 GS 上次見過
    last_seen = read_last_seen_from_gsheet()

    # 4) 比對新/變價
    def norm_price(s): return "".join(filter(str.isdigit,s))
    notify, new_keys = [], set()
    for _, r in df.iterrows():
        key = f"{r['name']}|{r['color']}|{norm_price(r['price'])}"
        if key not in last_seen:
            notify.append(f"[{r['source']}]\n{r['name']} {r['color']} {r['price']}\n{r['link']}")
            new_keys.add(key)

    # 5) 無新貨 → 只更新 GS → 結束
    if not notify:
        print("本次無新品／變價，僅更新 Google Sheets")
        write_current_seen_to_gsheet(df)
        sys.exit(0)

    # 6) 有新貨 → 更新 GS → Broadcast & Gmail
    write_current_seen_to_gsheet(df)
    message = "\n\n".join(notify)

    if CHANNEL_ACCESS_TOKEN:
        print("發送 LINE Broadcast")
        send_line_broadcast_message(message)
    if GMAIL_USER and GMAIL_APP_PASSWORD and GMAIL_TO:
        print("發送 Gmail")
        send_gmail("Hermès 新品／變價通知", message)

    print("所有通知完成！")

if __name__ == "__main__":
    main()
