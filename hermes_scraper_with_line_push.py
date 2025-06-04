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

# 設定日誌記錄
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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

# ====== Gmail 通知 ======
def send_gmail(subject, body):
    try:
        yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
        yag.send(GMAIL_TO, subject, body)
        logging.info("Gmail 通知已發送")
    except Exception as e:
        logging.error(f"發送 Gmail 失敗: {e}")

# ===== Google Sheets 客戶端與讀寫 ======
def get_gsheet_client():
    """
    從環境變數 GOOGLE_CREDS_JSON 讀取 Service Account JSON，
    授權並返回 gspread client。
    """
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
        logging.error(f"Google Sheets 驗證失敗: {e}")
        raise

def read_last_seen_from_gsheet():
    """
    開啟指定工作表 (GSHEET_ID)，讀取 Sheet1 的所有列，
    將 'name|color|price' 組合放入 set 後回傳。
    若失敗 (token 錯誤、網路問題)，就回傳空集合。
    """
    logging.info("進入 read_last_seen_from_gsheet()")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")
        data = ws.get_all_records()
        logging.info(f"[讀取] Google Sheet 共 {len(data)} 筆")
        last_set = set()
        for row in data:
            key = f"{row.get('name','')}|{row.get('color','')}|{row.get('price','')}"
            last_set.add(key)
        return last_set
    except Exception as e:
        logging.error(f"GS read failed: {e}", exc_info=True) # 記錄更詳細的錯誤堆疊
        return set()

def write_current_seen_to_gsheet(df):
    """
    將整個 DataFrame (hermes_data + 欄位) 一次性寫入 Sheet1。
    先以 .clear() 清空，然後用 ws.update(...) 一次性寫入所有格子，
    避免大量 append_row 而觸發 Google Sheets API rate limit。
    """
    logging.info("==== 準備寫入 Google Sheets ====")
    logging.debug(f"要寫入的 DataFrame 前幾行:\n{df.head()}")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")

        all_values = [df.columns.tolist()] + df.values.tolist()

        logging.info("正在清空 Google Sheet...")
        ws.clear()
        logging.info("Google Sheet 已清空。")

        max_row = len(all_values)
        cell_range = f"A1:F{max_row}"
        logging.info(f"正在寫入資料到 Google Sheet，範圍：{cell_range}...")
        ws.update(values=all_values, values=all_values, range_name=cell_range) # 修正重複的 values
        logging.info("==== 已經寫入 Google Sheets ====")
    except Exception as e:
        logging.error(f"寫入 Google Sheets 失敗: {e}", exc_info=True)
    finally:
        logging.info("【Debug結束】write_current_seen_to_gsheet 執行到最後")

def get_element_with_wait(driver, by, value, timeout=10):
    """使用 WebDriverWait 獲取元素，並處理超時異常"""
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )
    except TimeoutException:
        logging.warning(f"找不到元素 (超時): {by}={value}")
        return None
    except NoSuchElementException:
        logging.warning(f"找不到元素: {by}={value}")
        return None

def normalize_price(price_str):
    """提取價格字串中的數字部分"""
    return ''.join(filter(str.isdigit, price_str))

# ===== 主流程：爬取 Hermès 官網 + 去重 + 通知 =====
def main():
    # 1. 用 Selenium 抓取 Hermès 官網的「包包&手拿包」與「小皮件」
    hermes_data = []
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")

    # 設定 User-Agent
    ua = UserAgent()
    user_agent = ua.random
    chrome_options.add_argument(f'--user-agent={user_agent}')
    logging.info(f"使用的 User-Agent: {user_agent}")

    # 阻止載入圖片
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    logging.info("已禁用圖片載入")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options
    )

    # 設置頁面載入超時時間
    driver.set_page_load_timeout(30)

    for cname, url in hermes_urls:
        logging.info(f"抓取 Hermès 分類: {cname} - {url}")
        try:
            driver.get(url)
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item")))
            time.sleep(2)  # 額外等待一下確保動態內容載入
        except TimeoutException:
            logging.error(f"載入 {url} 超時")
            continue

        items = driver.find_elements(By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item")
        logging.info(f"► 本次共抓到 {len(items)} 件 Hermès 「{cname}」")

        for item in items:
            link = name = color = price = img = ""
            try:
                name_element = get_element_with_wait(item, By.CSS_SELECTOR, ".product-item-name span")
                if name_element:
                    name = name_element.text.strip()

                link_element = get_element_with_wait(item, By.CSS_SELECTOR, ".product-item-name", timeout=5)
                if link_element:
                    raw_href = link_element.get_attribute("href")
                    link = "https://www.hermes.com" + raw_href if raw_href.startswith("/") else raw_href

                color_element = get_element_with_wait(item, By.CSS_SELECTOR, ".product-item-colors", timeout=3)
                if color_element:
                    color = color_element.text.strip().replace("顏色:", "").strip()

                price_element = get_element_with_wait(item, By.CSS_SELECTOR, ".price", timeout=3)
                if price_element:
                    price = price_element.text.strip()

                img_element = get_element_with_wait(item, By.CSS_SELECTOR, "img", timeout=3)
                if img_element:
                    raw_src = img_element.get_attribute("src")
                    img = "https:" + raw_src if raw_src.startswith("//") else raw_src

            except Exception as e:
                logging.error(f"處理商品時發生錯誤: {e}")
                continue

            if name and link:
                hermes_data.append({
                    "source": f"Hermès官網 {cname}",
                    "name":   name,
                    "color":  color,
                    "price":  price,
                    "link":   link,
                    "img":    img,
                })

    driver.quit()

    # 2. 將蒐到的資料塞進 DataFrame，順序記得要有 color 這一欄
    df = pd.DataFrame(hermes_data, columns=["source","name","color","price","link","img"])

    # 3. 讀取 Google Sheets 上次已見 (name|color|price) 鍵值
    last_set = read_last_seen_from_gsheet()

    # 4. 逐筆比對：若 name|color|price 不在 last_set，就當作「新品/變價」
    notify_list = []
    new_keys = set()
    for _, row in df.iterrows():
        key = f"{row['name']}|{row['color']}|{normalize_price(row['price'])}"
        if key not in last_set:
            logging.info(f"發現新商品/變價：{key}")
            logging.debug(f"last_set 內容：{last_set}")
            notify_list.append(
                f"[{row['source']}]\n{row['name']} {row.get('color','')} {row['price']}\n{row['link']}"
            )
            new_keys.add(key)
        else:
            logging.debug(f"商品已存在，跳過通知：{key}")

    # 5. 如果這次沒有任何新品或變價，就直接把整張表寫回 Google Sheets 並結束
    if not notify_list:
        logging.info("本次無新增或變價商品，跳過通知。")
        logging.debug(f"準備寫入 Google Sheet 的 DataFrame:\n{df}")
        write_current_seen_to_gsheet(df)
        sys.exit(0)

    # 6. 先把剛要通知的鍵值加進 last_set_temp，以避免同一筆在下一次又被算作「新貨」
    last_set_temp = last_set.copy()
    last_set_temp.update(new_keys)

    # 7. 將整個 DataFrame 一次性寫回 Google Sheets（Sheet1）
    logging.debug(f"準備寫入 Google Sheet 的 DataFrame:\n{df}")
    write_current_seen_to_gsheet(df)

    # 8. 合併通知文字
    notify_msg = "\n\n".join(notify_list)

    # 9. 以 Broadcast 方式發送 LINE 訊息（給所有追蹤者）
    if CHANNEL_ACCESS_TOKEN:
        logging.info("發送 LINE Broadcast 訊息")
        send_line_broadcast_message(notify_msg)

    # 10. 以 Gmail 寄出通知
    if GMAIL_USER and GMAIL_TO and GMAIL_APP_PASSWORD:
        logging.info("發送 GMAIL 通知")
        send_gmail("Hermès 新上架／變價通知", notify_msg)
    elif GMAIL_USER:
        logging.warning("GMAIL_TO 或 GMAIL_APP_PASSWORD 未設定，跳過 GMAIL 通知")
    else:
        logging.warning("GMAIL_USER 未設定，跳過 GMAIL 通知")

    logging.info("只推播 Hermès 新品／變價商品（Broadcast + Google Sheets 記憶版）完成！")


if __name__ == "__main__":
    main()
