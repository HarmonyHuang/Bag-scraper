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
# 允許通過環境變數 LOG_LEVEL 來控制日誌級別 (例如: INFO, WARNING, ERROR, DEBUG)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ==== 環境變數 (必須在 GitHub Secrets 或本機 export 好) ====
# 使用 None 作為預設值，以便更明確地檢查是否已設定
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GMAIL_USER             = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD     = os.getenv("GMAIL_APP_PASSWORD")
GMAIL_TO               = os.getenv("GMAIL_TO")
GSHEET_ID              = os.getenv("GSHEET_ID")
GOOGLE_CREDS_JSON      = os.getenv("GOOGLE_CREDS_JSON")

# ===== 要爬的 Hermès 官網分類 =====
hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件",      "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]


# ====== LINE Broadcast (推播給所有追蹤者)，自動拆段超長訊息 ======
def send_line_broadcast_message(text):
    # 檢查 LINE_CHANNEL_ACCESS_TOKEN 是否已設定
    if not CHANNEL_ACCESS_TOKEN:
        logging.warning("LINE_CHANNEL_ACCESS_TOKEN 尚未設定，跳過 LINE Broadcast。")
        return []

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
        try:
            r = requests.post(url, headers=headers, json=payload)
            logging.info(f"LINE Broadcast 回應: {r.status_code} {r.text}")
            return [r.status_code]
        except requests.exceptions.RequestException as e:
            logging.error(f"發送 LINE Broadcast 失敗: {e}", exc_info=True)
            return []

    # 文字超長，自動拆段推播
    status_codes = []
    for part in chunk_text(text):
        payload = {"messages": [{"type": "text", "text": part}]}
        try:
            r = requests.post(url, headers=headers, json=payload)
            logging.info(f"LINE Broadcast 拆段回應: {r.status_code} {r.text}")
            status_codes.append(r.status_code)
            time.sleep(0.5)  # 拆段之間稍微延遲，避免瞬間大量呼叫
        except requests.exceptions.RequestException as e:
            logging.error(f"發送 LINE Broadcast 拆段失敗: {e}", exc_info=True)
            status_codes.append(500) # 標記為失敗
            break # 失敗後停止繼續發送
    return status_codes


# ====== Gmail 通知 ======
def send_gmail(subject, body):
    # 檢查所有 Gmail 相關環境變數是否已設定
    if not all([GMAIL_USER, GMAIL_TO, GMAIL_APP_PASSWORD]):
        logging.warning("Gmail 憑證 (GMAIL_USER, GMAIL_TO, 或 GMAIL_APP_PASSWORD) 未完全設定，跳過 Gmail 通知。")
        return

    try:
        yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
        yag.send(GMAIL_TO, subject, body)
        logging.info("Gmail 通知已發送")
    except Exception as e:
        logging.error(f"發送 Gmail 失敗: {e}", exc_info=True)


# ===== Google Sheets：建立 client & 讀／寫函式 ======
def get_gsheet_client():
    logging.info("進入 get_gsheet_client()")
    # 檢查 Google Sheets 相關環境變數是否已設定
    if not all([GSHEET_ID, GOOGLE_CREDS_JSON]):
        logging.error("Google Sheets 憑證 (GSHEET_ID 或 GOOGLE_CREDS_JSON) 未設定，無法建立客戶端。")
        raise ValueError("Google Sheets 憑證未設定。")

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
    except json.JSONDecodeError as e:
        logging.error(f"GOOGLE_CREDS_JSON 不是有效的 JSON 字串: {e}", exc_info=True)
        send_gmail("Hermès 爬蟲錯誤", f"Google Sheets 憑證 JSON 解析失敗: {e}") # 增加錯誤通知
        raise
    except Exception as e:
        logging.error(f"Google Sheets 驗證失敗: {e}", exc_info=True)
        send_gmail("Hermès 爬蟲錯誤", f"Google Sheets 驗證失敗: {e}") # 增加錯誤通知
        raise


def read_last_seen_from_gsheet():
    logging.info("進入 read_last_seen_from_gsheet()")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")
        data = ws.get_all_records()
        logging.info(f"[讀取] Google Sheet 共 {len(data)} 筆資料。")
        last_set = set()
        for row in data:
            # key = name|color|price
            key = f"{row.get('name','')}|{row.get('color','')}|{row.get('price','')}"
            last_set.add(key)
        return last_set
    except Exception as e:
        logging.error(f"讀取 Google Sheets 失敗: {e}", exc_info=True)
        send_gmail("Hermès 爬蟲錯誤", f"讀取 Google Sheets 失敗: {e}") # 增加錯誤通知
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
        send_gmail("Hermès 爬蟲錯誤", f"寫入 Google Sheets 失敗: {e}") # 增加錯誤通知
    finally:
        # 移除這個 Debug 訊息，因為它在每次寫入後都會出現，可能造成混淆
        # logging.info("【Debug結束】write_current_seen_to_gsheet 執行到最後")
        pass


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
    except Exception as e:
        logging.error(f"等待元素時發生未知錯誤 ({by}={value}): {e}", exc_info=True)
        return None


def normalize_price(price_str):
    """把價格字串裡所有非數字的字元過濾，只剩下數字。"""
    if not isinstance(price_str, str):
        return "" # 確保輸入是字串
    return "".join(filter(str.isdigit, price_str))


def extract_color(item):
    """
    從商品元素中提取顏色資訊。
    根據提供的圖片，顏色資訊可能在 .product-item-colors 類別下的 span 標籤中。
    """
    try:
        # 嘗試找到 .product-item-colors 類別下的 span 元素
        color_el = item.find_element(By.CSS_SELECTOR, ".product-item-colors span")
        return color_el.text.strip().replace("顏色:", "").strip()
    except NoSuchElementException:
        logging.debug("找不到顏色元素 .product-item-colors span。")
        return "" # 如果找不到顏色元素，返回空字串
    except Exception as e:
        logging.error(f"提取顏色時發生例外: {e}", exc_info=True)
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
    chrome_options.add_argument("--window-size=1920,1080") # 設定視窗大小，有助於確保元素可見

    # 隨機一個 User-Agent
    ua = UserAgent()
    user_agent = ua.random
    chrome_options.add_argument(f"--user-agent={user_agent}")
    logging.info(f"使用的 User-Agent：{user_agent}")

    # 禁掉圖片載入，加快爬頁速度
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    logging.info("已禁用圖片載入。")

    driver = None # 初始化 driver 為 None，確保在 finally 中可以正確判斷
    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=chrome_options
        )
        driver.set_page_load_timeout(60) # 增加頁面加載超時時間，以應對慢速網路或網站響應

        for cname, url in hermes_urls:
            logging.info(f"開始抓取 Hermès 分類：{cname} → {url}")
            try:
                driver.get(url)
                # 等待「商品卡片容器」出現 (product-grid-list-item OR product-grid-item)
                # 增加等待時間，並等待至少一個商品卡片容器出現
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item"))
                )
                # 額外等待一小段時間，確保所有 JS 內容渲染完成，但盡量避免靜態等待
                # time.sleep(2) # 考慮移除或替換為更精確的等待條件，例如等待滾動條出現或頁面高度穩定
            except TimeoutException:
                logging.error(f"載入 {cname} 頁面超時 (20秒)，跳過這個分類。", exc_info=True)
                send_gmail("Hermès 爬蟲警告", f"載入 {cname} 頁面超時，可能導致資料不完整。") # 增加警告通知
                continue
            except Exception as e:
                logging.error(f"載入 {cname} 頁面時發生其他錯誤: {e}，跳過這個分類。", exc_info=True)
                send_gmail("Hermès 爬蟲錯誤", f"載入 {cname} 頁面時發生錯誤: {e}") # 增加錯誤通知
                continue

            # 同時選出「舊版」和「新版」商品卡片
            items = driver.find_elements(By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item")
            logging.info(f"► 本次在「{cname}」分類共抓到 {len(items)} 件商品。")

            for item in items:
                name = link = color = price = img = ""
                try:
                    # 1) name: 產品名稱通常在 .product-item-name 類別下的 span 元素
                    name_el = get_element_with_wait(item, By.CSS_SELECTOR, ".product-item-name span", timeout=5)
                    if name_el:
                        name = name_el.text.strip()

                    # 2) link: 產品連結通常在 .product-item-name 類別下的 a 元素
                    link_el = get_element_with_wait(item, By.CSS_SELECTOR, ".product-item-name a", timeout=5)
                    if link_el:
                        raw_href = link_el.get_attribute("href")
                        if raw_href and raw_href.startswith("/"):
                            link = "https://www.hermes.com" + raw_href
                        else:
                            link = raw_href
                        # 如果連結為空，則跳過該商品
                        if not link:
                            logging.warning(f"商品 '{name}' 連結為空，跳過此商品。")
                            continue

                    # 3) color: 提取顏色資訊 (已啟用)
                    color = extract_color(item)

                    # 4) price: 價格通常在 span.price 元素
                    price_el = get_element_with_wait(item, By.CSS_SELECTOR, "span.price", timeout=3)
                    if price_el:
                        price = price_el.text.strip()

                    # 5) img: 圖片連結通常在 img 元素
                    img_el = get_element_with_wait(item, By.CSS_SELECTOR, "img", timeout=3)
                    if img_el:
                        raw_src = img_el.get_attribute("src")
                        if raw_src and raw_src.startswith("//"):
                            img = "https:" + raw_src
                        else:
                            img = raw_src

                    logging.debug(f"成功處理商品 → 名稱：{name}；顏色：{color}；價格：{price}；連結：{link}")
                except Exception as e:
                    logging.error(f"處理單筆商品時發生例外：{e} (商品可能不完整或找不到關鍵元素)。", exc_info=True)
                    continue

                if name and link: # 確保有名稱和連結才將商品加入列表
                    hermes_data.append({
                        "source": f"Hermès官網 {cname}",
                        "name":    name,
                        "color":   color,
                        "price":   price,
                        "link":    link,
                        "img":     img,
                    })
    finally:
        if driver:
            driver.quit() # 確保瀏覽器在任何情況下都會關閉
        logging.debug(f"所有分類爬取完畢，hermes_data 共 {len(hermes_data)} 筆資料。")
        if hermes_data:
            logging.debug(f"前五筆資料：\n{hermes_data[:5]}")
        else:
            logging.debug("hermes_data 為空。")


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
    new_keys = set() # 實際新增的商品或變價商品的 key

    for _, row in df.iterrows():
        # 標準化價格再進行比較，確保比較的準確性
        normalized_current_price = normalize_price(row['price'])
        key = f"{row['name']}|{row['color']}|{normalized_current_price}"

        if key not in last_set:
            logging.info(f"發現新品／變價 => {key}")
            notify_list.append(
                f"✨ 新品/變價通知 ✨\n"
                f"來源: {row['source']}\n"
                f"名稱: {row['name']}\n"
                f"顏色: {row.get('color','N/A')}\n" # 如果顏色為空，顯示 N/A
                f"價格: {row['price']}\n"
                f"連結: {row['link']}\n"
                f"圖片: {row['img'] if row['img'] else '無圖片'}" # 如果無圖片，顯示無圖片
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
    # 6. 把「本次完整的 df」寫回 Google Sheets (覆蓋整張表)
    # ------------------------------------------------------------
    # 這裡不再需要 last_set_temp，因為我們總是將最新的完整資料寫入 Google Sheets
    logging.debug(f"準備將本次爬取到的完整 DataFrame 寫入 Google Sheets。")
    write_current_seen_to_gsheet(df)

    # ------------------------------------------------------------
    # 7. 合併通知字串，並 BroadCast 給所有追蹤者
    # ------------------------------------------------------------
    notify_msg = "\n\n---\n\n".join(notify_list) # 使用更明顯的分隔符

    # LINE Broadcast
    logging.info("嘗試發送 LINE Broadcast 訊息...")
    send_line_broadcast_message(notify_msg)

    # Gmail
    logging.info("嘗試發送 Gmail 通知...")
    send_gmail("Hermès 新品／變價通知", notify_msg)

    logging.info("Hermès 新品／變價商品通知流程（Broadcast + Google Sheets 記憶版）完成！")


if __name__ == "__main__":
    main()
