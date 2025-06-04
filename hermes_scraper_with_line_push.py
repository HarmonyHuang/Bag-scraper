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

# ===== 設定日誌，方便追蹤執行流程 =====
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ==== 環境變數 (必須在 GitHub Secrets 或本機 export 好) ====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GMAIL_USER            = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD    = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO              = os.getenv("GMAIL_TO", "")
GSHEET_ID             = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON     = os.getenv("GOOGLE_CREDS_JSON", "")

# ===== 要爬的 Hermès 官網分類 =====
hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件",     "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]


# ====== LINE Broadcast (推播給所有追蹤者)，自動拆段超長訊息 ======
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

    # 如果文字不超過單次上限就直接推播
    if len(text) <= MAX_LEN:
        payload = {"messages": [{"type": "text", "text": text}]}
        r = requests.post(url, headers=headers, json=payload)
        logging.info(f"LINE Broadcast 回應: {r.status_code} {r.text}")
        return [r.status_code]

    # 文字超長，自動拆段推播
    status_codes = []
    for part in chunk_text(text):
        payload = {"messages": [{"type": "text", "text": part}]}
        r = requests.post(url, headers=headers, json=payload)
        logging.info(f"LINE Broadcast 拆段回應: {r.status_code} {r.text}")
        status_codes.append(r.status_code)
        time.sleep(0.5)  # 拆段之間稍微延遲，避免瞬間大量呼叫
    return status_codes


# ====== Gmail 通知 ======
def send_gmail(subject, body):
    try:
        yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
        yag.send(GMAIL_TO, subject, body)
        logging.info("Gmail 通知已發送")
    except Exception as e:
        logging.error(f"發送 Gmail 失敗: {e}", exc_info=True)


# ===== Google Sheets：建立 client & 讀／寫函式 ======
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
        return set()  # 失敗就回傳空集合，讓程式繼續跑但當作是「第一次執行」


def write_current_seen_to_gsheet(df):
    logging.info("==== 準備寫入 Google Sheets ====")
    logging.debug(f"要寫入的 DataFrame 頭幾列:\n{df.head()}")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")

        # DataFrame 轉成二維陣列：先放欄位名稱，再把所有列都放進來
        all_values = [df.columns.tolist()] + df.values.tolist()

        logging.info("正在清空 Google Sheet...")
        ws.clear()
        logging.info("Google Sheet 已清空。")

        max_row = len(all_values)
        cell_range = f"A1:F{max_row}"
        logging.info(f"正在寫入資料到 Google Sheet，範圍：{cell_range}...")
        ws.update(values=all_values, range_name=cell_range)
        logging.info("==== 已經寫入 Google Sheets ====")
    except Exception as e:
        logging.error(f"寫入 Google Sheets 失敗: {e}", exc_info=True)
    finally:
        logging.info("【Debug結束】write_current_seen_to_gsheet 執行到最後")


def get_element_with_wait(context, by, value, timeout=10):
    """
    給定 driver (整個頁面) 或 WebElement (某個區塊) 做等待，
    若在 timeout 內找到就回傳元素，否則回 None。
    """
    try:
        return WebDriverWait(context, timeout).until(
            EC.presence_of_element_located((by, value))
        )
    except TimeoutException:
        logging.warning(f"找不到元素 (超時)：{by}={value}")
        return None
    except NoSuchElementException:
        logging.warning(f"找不到元素：{by}={value}")
        return None


def normalize_price(price_str):
    """把價格字串裡所有非數字的字元過濾，只剩下數字。"""
    return "".join(filter(str.isdigit, price_str))


def extract_color(item):
    """
    如果要真正抓「color」(例如 .product-item-colors)，可在此實作：
    例如：
        text = item.find_element(By.CSS_SELECTOR, ".product-item-colors").text
        return text.strip().replace("顏色:", "").strip()
    目前範例先回空字串(project demo 時再自行打開)
    """
    try:
        text = item.find_element(By.CSS_SELECTOR, ".product-item-colors").text
        return text.strip().replace("顏色:", "").strip()
    except Exception:
        return ""


def main():
    # ------------------------------------------------------------
    # 1. 用 Selenium 爬 Hermès 官網這兩個分類
    # ------------------------------------------------------------
    hermes_data = []

    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")

    # 隨機一個 User-Agent
    ua = UserAgent()
    user_agent = ua.random
    chrome_options.add_argument(f"--user-agent={user_agent}")
    logging.info(f"使用的 User-Agent：{user_agent}")

    # 禁掉圖片載入，加快爬頁速度
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
            # 等待「商品卡片容器」出現 (product-grid-list-item OR product-grid-item)
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item"))
            )
            time.sleep(5)  # 多留 5 秒確保 JS 動態渲染
        except TimeoutException:
            logging.error(f"載入 {cname} 頁面超時，跳過這個分類")
            continue

        # 同時選出「舊版」和「新版」商品卡片
        items = driver.find_elements(By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item")
        logging.info(f"► 本次共抓到 {len(items)} 件 Hermès 「{cname}」")

        for item in items:
            name = link = color = price = img = ""
            try:
                # 1) name
                name_el = get_element_with_wait(item, By.CSS_SELECTOR, ".product-item-name span", timeout=5)
                if name_el:
                    name = name_el.text.strip()

                # 2) link
                link_el = get_element_with_wait(item, By.CSS_SELECTOR, ".product-item-name", timeout=5)
                if link_el:
                    raw_href = link_el.get_attribute("href")
                    if raw_href and raw_href.startswith("/"):
                        link = "https://www.hermes.com" + raw_href
                    else:
                        link = raw_href

                # 3) color
                color = extract_color(item)

                # 4) price
                price_el = get_element_with_wait(item, By.CSS_SELECTOR, "span.price", timeout=3)
                if price_el:
                    price = price_el.text.strip()

                # 5) img
                img_el = get_element_with_wait(item, By.CSS_SELECTOR, "img", timeout=3)
                if img_el:
                    raw_src = img_el.get_attribute("src")
                    if raw_src and raw_src.startswith("//"):
                        img = "https:" + raw_src
                    else:
                        img = raw_src

                logging.debug(f"處理商品 → name：{name}；color：{color}；price：{price}；link：{link}")
            except Exception as e:
                logging.error(f"處理單筆商品時發生例外：{e}", exc_info=True)
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
    logging.debug(f"爬取完畢，hermes_data 共 {len(hermes_data)} 筆 → 頭五筆：\n{hermes_data[:5]}")

    # ------------------------------------------------------------
    # 2. 把爬到的結果存成 DataFrame
    # ------------------------------------------------------------
    df = pd.DataFrame(hermes_data, columns=["source","name","color","price","link","img"])

    # ------------------------------------------------------------
    # 3. 先判斷「今天到底爬到幾筆？」如果完全沒抓到，就直接寫入空表頭並結束
    # ------------------------------------------------------------
    if df.empty:
        logging.warning("!!! 本次 Selenium 並未抓到任何商品（hermes_data 為空），直接更新欄位名稱後結束程式。")
        write_current_seen_to_gsheet(df)  # 只更新標題列 A1:F1
        sys.exit(0)

    # ------------------------------------------------------------
    # 4. 讀 Google Sheets 上次已見：把 Sheet1 裡面所有 name|color|price 組成一個集合
    # ------------------------------------------------------------
    last_set = read_last_seen_from_gsheet()

    # ------------------------------------------------------------
    # 5. 比對「新品/變價」邏輯
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
            logging.debug(f"商品已在上次列表中，跳過：{key}")

    # 如果「沒有任何要通知的新品/變價」，就直接把整張表（含所有商品）寫回 Sheet，然後結束
    if not notify_list:
        logging.info("本次無新增或變價商品，跳過通知。")
        write_current_seen_to_gsheet(df)
        sys.exit(0)

    # ------------------------------------------------------------
    # 6. 把新 key 暫時加入 last_set_temp（避免同一次內重複通知），但最重要的是：
    #    * 先把「本次完整的 df」寫回 Google Sheets (覆蓋整張表)
    # ------------------------------------------------------------
    last_set_temp = last_set.copy()
    last_set_temp.update(new_keys)

    logging.debug(f"準備寫入 Google Sheets 的 DataFrame 頭兩筆：\n{df.head(2)}")
    write_current_seen_to_gsheet(df)

    # ------------------------------------------------------------
    # 7. 合併通知字串，並 BroadCast 給所有追蹤者
    # ------------------------------------------------------------
    notify_msg = "\n\n".join(notify_list)

    # LINE Broadcast
    if CHANNEL_ACCESS_TOKEN:
        logging.info("發送 LINE Broadcast 訊息")
        send_line_broadcast_message(notify_msg)
    else:
        logging.warning("LINE_CHANNEL_ACCESS_TOKEN 尚未設定，跳過 Broadcast。")

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
