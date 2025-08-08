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

# ==== 環境變數 (GitHub Secrets 或本地 Export) ====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GMAIL_USER            = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD    = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO              = os.getenv("GMAIL_TO", "")
GSHEET_ID             = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON     = os.getenv("GOOGLE_CREDS_JSON", "")

# ===== Hermès 官網 要爬的分類（同時抓「包包&手拿包」和「小皮件」） =====
hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件",     "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

# ====== LINE Broadcast (推送給所有追蹤者)，並自動拆段超長文字 ======
def send_line_broadcast_message(text):
    """
    以 broadcast 方式推播給所有追蹤此官方帳號的用戶。
    必須在 LINE 官方帳號的 Messaging API 設定中「允許 Broadcast」並使用付費方案。
    內建自動將超過 5000 bytes 的文字拆成多段，每段不超過 4900 bytes 再逐一送出。
    """
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    MAX_LEN = 4900  # LINE API 單次限制約 5000 bytes，留些 margin

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

# ====== Gmail 通知 ======
def send_gmail(subject, body):
    yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
    yag.send(GMAIL_TO, subject, body)

# ===== Google Sheets 客戶端與讀寫 ======
def get_gsheet_client():
    """
    從環境變數 GOOGLE_CREDS_JSON 讀取 Service Account JSON，
    授權並返回 gspread client。
    """
    print("進入 get_gsheet_client()")
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    print("Google Sheets 驗證成功")
    return client

def read_last_seen_from_gsheet():
    """
    開啟指定工作表 (GSHEET_ID)，讀取 Sheet1 的所有列，
    將 'name|color|price' 組合放入 set 後回傳。
    若失敗 (token 錯誤、網路問題)，就回傳空集合。
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
            key = f"{row.get('name','')}|{row.get('color','')}|{row.get('price','')}"
            last_set.add(key)
        return last_set
    except Exception as e:
        print("GS read failed, fallback to empty set:", e)
        return set()

def write_current_seen_to_gsheet(df):
    """
    將整個 DataFrame (hermes_data + 欄位) 一次性寫入 Sheet1。
    先以 .clear() 清空，然後用 ws.update(...) 一次性寫入所有格子，
    避免大量 append_row 而觸發 Google Sheets API rate limit。
    """
    print("==== 準備寫入 Google Sheets ====")
    print(df.head())
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")

        # DataFrame 轉成二維陣列：第一列放欄位名稱 (source, name, color, price, link, img)
        all_values = [df.columns.tolist()] + df.values.tolist()

        # 一次性清空 + 批次更新
        ws.clear()
        max_row = len(all_values)
        cell_range = f"A1:F{max_row}"
        ws.update(values=all_values, range_name=cell_range)
        print("==== 已經寫入 Google Sheets ====")
    except Exception as e:
        print("寫入 Google Sheets 失敗:", e)
    finally:
        print("【Debug結束】write_current_seen_to_gsheet 執行到最後")

# ===== 主流程：用 requests + BeautifulSoup 爬取 Hermès 官網 + 去重 + 通知 =====
def main():
    hermes_data = []

    # 1. 用 requests + BeautifulSoup 取代 Selenium
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
    }

    for cname, url in hermes_urls:
        print(f"爬取 Hermès 分類: {cname} ({url})")
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            print(f"  → 無法成功載入 {url} (HTTP {resp.status_code})，跳過這個分類")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # Hermès 官網的商品通常落在 .product-grid-item 或 .product-grid-list-item
        items = soup.select("div.product-grid-list-item, div.product-grid-item")
        print(f"► 本次共抓到 {len(items)} 件 Hermès 「{cname}」")

        for item in items:
            # 解析每個商品區塊
            name = color = price = link = img = ""

            # 1) 連結 & 名稱
            a_tag = item.select_one(".product-item-name")
            if a_tag:
                raw_href = a_tag.get("href", "").strip()
                if raw_href.startswith("/"):
                    link = "https://www.hermes.com" + raw_href
                else:
                    link = raw_href
                # 名稱通常在 <span> 裡面
                span_name = a_tag.select_one("span")
                if span_name and span_name.get_text(strip=True):
                    name = span_name.get_text(strip=True)

            # 2) 顏色 (若存在的話)
            color_tag = item.select_one(".product-item-colors")
            if color_tag:
                color = color_tag.get_text(strip=True).replace("顏色:", "").strip()

            # 3) 價格
            price_tag = item.select_one("span.price")
            if price_tag:
                price = price_tag.get_text(strip=True)

            # 4) 圖片
            img_tag = item.select_one("img")
            if img_tag:
                raw_src = img_tag.get("src", "").strip()
                if raw_src.startswith("//"):
                    img = "https:" + raw_src
                else:
                    img = raw_src

            # 如果至少有 name 或 link 才納入
            if name or link:
                hermes_data.append({
                    "source": f"Hermès官網 {cname}",
                    "name":   name,
                    "color":  color,
                    "price":  price,
                    "link":   link,
                    "img":    img,
                })

        # 為了避免過度頻繁地向 Hermes 伺服器請求，可以睡 1 秒
        time.sleep(1)

    # 如果連任何商品都沒抓到
    if not hermes_data:
        print("!!! 注意：本次完全抓不到任何商品 (hermes_data 為空)，請檢查 CSS Selector 或 網頁結構是否改版！")
        # 不更新 Google Sheets，也不推播，直接結束
        sys.exit(0)

    # 2. 將蒐到的資料塞進 DataFrame，順序要有 color 這一欄
    df = pd.DataFrame(hermes_data, columns=["source","name","color","price","link","img"])

    # 3. 讀取 Google Sheets 上次已見 (name|color|price) 鍵值
    last_set = read_last_seen_from_gsheet()

    # 4. 逐筆比對：若 name|color|price 不在 last_set，就當作「新品/變價」
    notify_list = []
    new_keys = set()

    def normalize_price(p: str) -> str:
        # 把價格裡面非數字的字元都去掉，方便比對
        return "".join(filter(str.isdigit, p))

    for _, row in df.iterrows():
        p = normalize_price(row["price"])
        key = f"{row['name']}|{row['color']}|{p}"
        if key not in last_set:
            # 只有真的「舊表格沒出現過」才要推播
            notify_list.append(
                f"[{row['source']}]\n{row['name']} {row.get('color','')} {row['price']}\n{row['link']}"
            )
            new_keys.add(key)

    # 5. 如果這次沒有任何新品或變價，就直接把整張表寫回 Google Sheets 並結束
    if not notify_list:
        print("本次無新增或變價商品，跳過通知。")
        write_current_seen_to_gsheet(df)
        sys.exit(0)

    # 6. 把剛要通知的鍵值加進 last_set_temp，以避免同一次程式內重複通知
    last_set_temp = last_set.copy()
    last_set_temp.update(new_keys)

    # 7. 將整個 DataFrame 一次性寫回 Google Sheets（Sheet1）
    write_current_seen_to_gsheet(df)

    # 8. 合併通知文字
    notify_msg = "\n\n".join(notify_list)

    # 9. 以 Broadcast 方式發送 LINE 訊息（給所有追蹤者）
    if CHANNEL_ACCESS_TOKEN:
        print("發送 LINE Broadcast 訊息")
        send_line_broadcast_message(notify_msg)

    # 10. 以 Gmail 寄出通知
    if GMAIL_USER and GMAIL_TO and GMAIL_APP_PASSWORD:
        print("發送 GMAIL 通知")
        send_gmail("Hermès 新上架／變價通知", notify_msg)
    else:
        print("︳GMAIL 參數未完全設定，跳過 Gmail 通知。")

    print("只推播 Hermès 新品／變價商品（Broadcast + Google Sheets 記憶版）完成！")

if __name__ == "__main__":
    main()
