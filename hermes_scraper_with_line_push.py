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
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager
import pandas as pd
import yagmail
import gspread
import json
from google.oauth2.service_account import Credentials
from fake_useragent import UserAgent
import logging
import re # 導入正規表達式模組

# 設定日誌記錄
# 調整日誌級別為 INFO，避免輸出過多 DEBUG 訊息。如需詳細除錯，可改回 DEBUG。
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ==== 環境變數 (GitHub Secrets 或本地 Export) ====
# 這些變數應該從環境中獲取，例如 GitHub Secrets 或本地 shell 的 export 命令
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GMAIL_USER           = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD   = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO             = os.getenv("GMAIL_TO", "")
GSHEET_ID            = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON    = os.getenv("GOOGLE_CREDS_JSON", "") # 這是 JSON 字串，而非文件路徑

# ===== Hermès 官方網站 要爬的兩個分類 =====
hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    # 已檢查：確保這裡的字串是完整且正確封閉的
    ("小皮件",      "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

# ====== LINE Broadcast (推送給所有追蹤者)，並自動拆段超長文字 ======
def send_line_broadcast_message(text):
    """
    發送 LINE Broadcast 訊息，並自動將超長訊息拆分成多段。
    """
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    MAX_LEN = 4900 # LINE 訊息最大長度限制

    def chunk_text(long_text, chunk_size=MAX_LEN):
        """將長文本拆分成指定大小的塊"""
        chunks = []
        start = 0
        while start < len(long_text):
            chunks.append(long_text[start : start + chunk_size])
            start += chunk_size
        return chunks

    if not CHANNEL_ACCESS_TOKEN:
        logging.warning("LINE_CHANNEL_ACCESS_TOKEN 未設定，無法發送 LINE 訊息。")
        return [None] # 返回 None 表示未發送

    if len(text) <= MAX_LEN:
        # 訊息長度未超過限制，直接發送
        payload = {"messages": [{"type": "text", "text": text}]}
        try:
            r = requests.post(url, headers=headers, json=payload)
            r.raise_for_status() # 對於非 2xx 的狀態碼拋出 HTTPError
            logging.info(f"LINE Broadcast 回應: {r.status_code} {r.text}")
            return [r.status_code]
        except requests.exceptions.RequestException as e:
            logging.error(f"發送 LINE Broadcast 失敗: {e}", exc_info=True)
            return [None] # 返回 None 表示發送失敗
    else:
        # 訊息長度超過限制，拆分後分段發送
        status_codes = []
        for i, part in enumerate(chunk_text(text)):
            payload = {"messages": [{"type": "text", "text": part}]}
            try:
                r = requests.post(url, headers=headers, json=payload)
                r.raise_for_status()
                logging.info(f"LINE Broadcast 拆段 {i+1} 回應: {r.status_code} {r.text}")
                status_codes.append(r.status_code)
                time.sleep(0.5) # 每發送一段後暫停，避免觸發 LINE API 頻率限制
            except requests.exceptions.RequestException as e:
                logging.error(f"發送 LINE Broadcast 拆段 {i+1} 失敗: {e}", exc_info=True)
                status_codes.append(None)
                time.sleep(0.5) # 即使失敗也等待，避免頻繁請求
        return status_codes

# ====== Gmail 通知 ======
def send_gmail(subject, body):
    """
    發送 Gmail 通知郵件。
    """
    # 檢查所有必要的 Gmail 環境變數是否都已設定
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, GMAIL_TO]):
        logging.warning("Gmail 設定不完整，跳過 Gmail 通知。請檢查 GMAIL_USER, GMAIL_APP_PASSWORD, GMAIL_TO。")
        return

    try:
        yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
        yag.send(to=GMAIL_TO, subject=subject, contents=body)
        logging.info("Gmail 通知已發送")
    except Exception as e:
        logging.error(f"發送 Gmail 失敗: {e}", exc_info=True) # 打印完整的錯誤堆棧

# ===== Google Sheets 客戶端與讀寫 ======
def get_gsheet_client():
    """
    獲取 Google Sheets API 客戶端。
    """
    logging.info("進入 get_gsheet_client()")
    # 檢查 GOOGLE_CREDS_JSON 環境變數是否設定
    if not GOOGLE_CREDS_JSON:
        logging.error("GOOGLE_CREDS_JSON 環境變數未設定，無法連接 Google Sheets。")
        raise ValueError("Google 憑證 JSON 未設定")

    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON) # 將 JSON 字串解析為字典
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        logging.info("Google Sheets 驗證成功")
        return client
    except json.JSONDecodeError as e:
        logging.error(f"解析 GOOGLE_CREDS_JSON 失敗，請確認其為有效的 JSON 格式: {e}", exc_info=True)
        raise # 重新拋出異常，讓調用者知道配置有問題
    except Exception as e:
        logging.error(f"Google Sheets 驗證失敗: {e}", exc_info=True)
        raise # 重新拋出異常

def read_last_seen_from_gsheet():
    """
    從 Google Sheets 讀取上次已見過的商品資料集。
    """
    logging.info("進入 read_last_seen_from_gsheet()")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1") # 預設讀取 Sheet1
        data = ws.get_all_records() # 讀取所有記錄，作為字典列表
        logging.info(f"[讀取] Google Sheet 共 {len(data)} 筆資料。")
        last_set = set()
        for row in data:
            # 確保獲取鍵值時不會因 None 而報錯，並在拼接 key 時正規化價格
            name = row.get('name', '') if row.get('name') is not None else ''
            color = row.get('color', '') if row.get('color') is not None else ''
            price = row.get('price', '') if row.get('price') is not None else '' # price 可能不是字串，轉為字串
            key = f"{name}|{color}|{normalize_price(str(price))}"
            last_set.add(key)
        return last_set
    except gspread.exceptions.WorksheetNotFound:
        logging.error(f"Google Sheet '{GSHEET_ID}' 中的工作表 'Sheet1' 未找到。請確保工作表名稱正確。")
        return set() # 返回空集合，避免程式中斷
    except Exception as e:
        logging.error(f"讀取 Google Sheet 失敗: {e}", exc_info=True)
        return set() # 返回空集合，避免程式中斷

def write_current_seen_to_gsheet(df):
    """
    將當前爬取到的商品資料寫入 Google Sheets。
    """
    logging.info("==== 準備寫入 Google Sheets ====")
    if df.empty:
        logging.info("DataFrame 為空，跳過寫入 Google Sheets。")
        return

    logging.debug(f"要寫入的 DataFrame 前幾行:\n{df.head()}")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Sheet1")

        # 準備寫入的資料，包括標題行
        all_values = [df.columns.tolist()] + df.values.tolist()
        
        logging.info("正在清空 Google Sheet...")
        ws.clear() # 清空工作表所有內容
        logging.info("Google Sheet 已清空。")
        
        max_row = len(all_values)
        max_col = len(all_values[0]) if all_values else 0

        # 將數字列索引轉換為 Excel 的字母列名 (例如：1 -> A, 2 -> B)
        def col_to_excel_col(col_num):
            col_str = ""
            while col_num > 0:
                col_num, remainder = divmod(col_num - 1, 26) # divmod 返回商和餘數
                col_str = chr(65 + remainder) + col_str # 65 是 'A' 的 ASCII 值
            return col_str

        # 計算寫入的範圍，例如 A1:F100
        cell_range = f"A1:{col_to_excel_col(max_col)}{max_row}"
        
        logging.info(f"正在寫入資料到 Google Sheet，範圍：{cell_range}...")
        # 批量更新工作表
        ws.update(values=all_values, range_name=cell_range)
        logging.info("==== 已經寫入 Google Sheets ====")
    except Exception as e:
        logging.error(f"寫入 Google Sheets 失敗: {e}", exc_info=True)
    finally:
        logging.info("【Debug結束】write_current_seen_to_gsheet 執行到最後")

def get_element_with_wait(parent_element, by, value, timeout=10):
    """
    嘗試定位元素，並等待其出現。
    支援在 driver 或特定 WebElement 內部查找元素。
    如果找不到元素，會記錄警告並返回 None。
    """
    try:
        # 如果 parent_element 是 WebDriver 實例，則直接使用 WebDriverWait 等待元素在整個頁面中出現
        if isinstance(parent_element, webdriver.remote.webdriver.WebDriver):
            return WebDriverWait(parent_element, timeout).until(
                EC.presence_of_element_located((by, value))
            )
        # 如果 parent_element 是 WebElement 實例，則在其內部查找子元素
        else:
            # WebDriverWait 不直接支持在 WebElement 內部查找。
            # 我們需要手動循環等待直到元素出現或超時。
            end_time = time.time() + timeout
            while time.time() < end_time:
                try:
                    # 在父元素內部查找子元素
                    element = parent_element.find_element(by, value)
                    return element
                except NoSuchElementException:
                    time.sleep(0.2) # 短暫等待後重試
            # 如果循環結束仍未找到，則拋出 TimeoutException
            raise TimeoutException(f"在父元素中找不到元素 (超時): {by}={value}")
    except TimeoutException:
        logging.warning(f"找不到元素 (超時): {by}={value}，在父元素 {parent_element} 中。")
        return None
    except NoSuchElementException:
        logging.warning(f"找不到元素: {by}={value}，在父元素 {parent_element} 中。")
        return None
    except Exception as e:
        logging.error(f"查找元素時發生未知錯誤: {e}", exc_info=True)
        return None

def normalize_price(price_str):
    """
    從價格字串中提取所有數字並返回。
    例如："NT$ 123,456" -> "123456"
    """
    if not isinstance(price_str, str):
        return "" # 如果輸入不是字串，返回空字串
    # 使用正規表達式匹配所有數字並連接起來
    return re.sub(r'[^\d]', '', price_str)

def extract_color(item):
    """
    目前不從商品項目中提取顏色資訊，直接返回空字串。
    如果你需要提取顏色，可以在此處添加解析邏輯。
    """
    return ""

def setup_driver():
    """
    設定並返回一個配置好的 Selenium WebDriver 實例。
    包含無頭模式、禁用沙盒、禁用 GPU、隨機 User-Agent 等配置。
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless") # 無頭模式，不顯示瀏覽器視窗
    chrome_options.add_argument("--no-sandbox") # 禁用沙盒模式，在高權限環境下可能需要
    chrome_options.add_argument("--disable-dev-shm-usage") # 解決 /dev/shm 空間不足問題，在 Docker 環境中常見
    chrome_options.add_argument("--disable-gpu") # 禁用 GPU 硬體加速，有時可解決在 Docker 或無頭環境中的問題
    chrome_options.add_argument("--window-size=1920,1080") # 設定視窗大小，模擬真實瀏覽器行為

    # 使用 fake_useragent 生成隨機 User-Agent，模擬真實用戶訪問，減少被反爬的機率
    ua = UserAgent()
    user_agent = ua.random
    chrome_options.add_argument(f'--user-agent={user_agent}')
    logging.info(f"使用的 User-Agent: {user_agent}")

    # 禁用圖片載入以加速頁面載入和減少數據量
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    logging.info("已禁用圖片載入以加速爬取。")

    try:
        # 使用 ChromeDriverManager 自動下載並管理 ChromeDriver
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=chrome_options
        )
        driver.set_page_load_timeout(30) # 設定頁面載入超時時間為 30 秒
        return driver
    except WebDriverException as e:
        logging.critical(f"初始化 WebDriver 失敗: {e}. 請檢查 ChromeDriver 安裝或版本是否正確。", exc_info=True)
        sys.exit(1) # 如果 WebDriver 無法啟動，則終止程式

def main():
    """
    主程序，執行商品資訊的爬取、比較、通知和資料儲存。
    """
    hermes_data = [] # 用於儲存爬取到的所有商品數據
    driver = None # 初始化 driver 變數為 None，確保在 finally 塊中可以判斷

    try:
        driver = setup_driver() # 初始化 WebDriver
        for cname, url in hermes_urls:
            logging.info(f"抓取 Hermès 分類: {cname} - {url}")
            try:
                driver.get(url) # 訪問目標 URL
                # 等待商品列表或產品網格元素載入完成
                # 這裡使用多個選擇器，增加對網站結構變動的適應性
                WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item, div.product-list-item")))
                time.sleep(3) # 給予頁面更多時間載入內容，以防 JavaScript 渲染延遲或非同步加載
            except TimeoutException:
                logging.error(f"載入 {url} 超時或找不到商品列表元素，跳過此分類。")
                continue # 跳過當前 URL，繼續下一個
            except WebDriverException as e:
                logging.error(f"在載入 {url} 時發生 WebDriver 錯誤: {e}", exc_info=True)
                continue # 跳過當前 URL，繼續下一個

            # 查找所有商品項目
            items = driver.find_elements(By.CSS_SELECTOR, "div.product-grid-list-item, div.product-grid-item, div.product-list-item")
            logging.info(f"► 本次共抓到 {len(items)} 件 Hermès 「{cname}」商品。")
            
            if not items:
                logging.warning(f"在 {url} 未找到任何商品項目。可能網站結構有變動或該分類暫無商品。")
                continue

            for i, item in enumerate(items):
                link = name = color = price = img = "" # 初始化商品屬性
                try:
                    # 獲取商品名稱：嘗試多種 CSS 選擇器，增加健壯性
                    name_element = get_element_with_wait(item, By.CSS_SELECTOR, ".product-item-name span, .product-item__name, .product-item-title, .product-card__name")
                    if name_element:
                        name = name_element.text.strip()
                    else:
                        logging.warning(f"商品 {i+1} 未找到名稱元素，跳過此商品。")
                        continue # 如果沒有名稱，則跳過此商品

                    # 獲取商品連結：嘗試多種 CSS 選擇器
                    link_element = get_element_with_wait(item, By.CSS_SELECTOR, "a.product-item-name, a.product-item__link, a.grid-item-link, a.product-card__link", timeout=5)
                    if link_element:
                        raw_href = link_element.get_attribute("href")
                        if raw_href:
                            # 處理相對路徑或不完整的連結，確保是完整的 HTTPS 連結
                            if raw_href.startswith('/'):
                                link = "https://www.hermes.com" + raw_href
                            elif not raw_href.startswith('http'): # 如果不是 http 開頭，可能需要補全
                                link = "https://" + raw_href
                            else:
                                link = raw_href
                        else:
                            logging.warning(f"商品 '{name}' 未找到連結屬性，跳過。")
                            continue
                    else:
                        logging.warning(f"商品 '{name}' 未找到連結元素，跳過。")
                        continue

                    color = extract_color(item) # 提取顏色 (目前返回空字串)

                    # 獲取商品價格：嘗試多種 CSS 選擇器
                    price_element = get_element_with_wait(item, By.CSS_SELECTOR, "span.price, span.price__value, .product-item-price .price-value, .product-card__price", timeout=3)
                    if price_element:
                        price = price_element.text.strip()
                    else:
                        logging.warning(f"商品 '{name}' 未找到價格元素。")

                    # 獲取商品圖片連結：嘗試多種 CSS 選擇器
                    img_element = get_element_with_wait(item, By.CSS_SELECTOR, "img.product-item-image, img.product-item__image, img.lazyloaded, img[data-src]", timeout=3)
                    if img_element:
                        # 檢查 src 或 data-src 屬性，有些圖片是懶加載的
                        raw_src = img_element.get_attribute("src") or img_element.get_attribute("data-src") 
                        if raw_src:
                            # 處理圖片連結，確保是完整的 HTTPS 連結
                            if raw_src.startswith('//'):
                                img = "https:" + raw_src
                            elif not raw_src.startswith('http'):
                                img = "https://" + raw_src # 假設是相對路徑，補全協議
                            else:
                                img = raw_src
                        else:
                            logging.warning(f"商品 '{name}' 未找到圖片來源屬性。")
                    else: 
                        logging.warning(f"商品 '{name}' 未找到圖片元素。")

                    logging.debug(f"處理商品：{name}")
                    logging.debug(f"  連結：{link}")
                    logging.debug(f"  顏色：{color}")
                    logging.debug(f"  價格：{price}")
                    logging.debug(f"  圖片連結：{img}")

                except Exception as e:
                    logging.error(f"處理商品 '{name}' (索引 {i}) 時發生錯誤: {e}", exc_info=True)
                    continue # 遇到錯誤時繼續處理下一個商品

                if name and link: # 確保商品名稱和連結都存在才加入數據列表
                    hermes_data.append({
                        "source": f"Hermès官網 {cname}", # 來源，包含分類名稱
                        "name":    name,
                        "color":   color,
                        "price":   price,
                        "link":    link,
                        "img":     img,
                    })
            
            time.sleep(2) # 在每個分類抓取完畢後，可以加入一些延遲，避免過於頻繁的請求

    except Exception as e:
        logging.critical(f"主程序執行時發生嚴重錯誤: {e}", exc_info=True)
        # 如果發生嚴重錯誤，發送 Gmail 異常通知
        send_gmail("Hermès 追蹤程式異常", f"程式執行時發生錯誤: {e}\n\n請檢查日誌了解詳情。")
        sys.exit(1) # 發生嚴重錯誤時，終止程式執行

    finally:
        # 確保在任何情況下都關閉 WebDriver 瀏覽器實例
        if driver:
            driver.quit()
            logging.info("WebDriver 已關閉。")

    logging.debug(f"爬取到的原始資料 hermes_data: {hermes_data}")
    # 將爬取到的數據轉換為 DataFrame，並指定列順序
    df = pd.DataFrame(hermes_data, columns=["source","name","color","price","link","img"])

    if df.empty:
        logging.info("本次爬取沒有獲取到任何商品數據，跳過後續處理。")
        sys.exit(0) # 如果沒有數據，直接退出

    # 從 Google Sheets 讀取上次已見過的商品數據
    last_set = read_last_seen_from_gsheet()
    notify_list = [] # 儲存需要通知的新商品或變價商品
    current_keys = set() # 記錄本次爬取到的所有商品的唯一鍵

    # 遍歷當前爬取到的數據，檢查是否有新商品或價格變動
    for _, row in df.iterrows():
        # 創建商品的唯一鍵 (名稱|顏色|正規化價格)
        key = f"{row['name']}|{row['color']}|{normalize_price(row['price'])}"
        current_keys.add(key) # 將當前商品加入到本次爬取到的 key 集合中

        if key not in last_set:
            # 如果這個 key 不在上次的記錄中，則說明是新商品或價格變動
            logging.info(f"發現新商品/變價：{key}")
            notify_list.append(
                f"[{row['source']}]\n商品名稱: {row['name']}\n顏色: {row.get('color','')}\n價格: {row['price']}\n連結: {row['link']}\n圖片: {row['img']}"
            )
        else:
            logging.debug(f"商品已存在，跳過通知：{key}")

    if not notify_list:
        logging.info("本次無新增或變價商品，跳過通知。")
        # 即使沒有新商品，也更新 Google Sheet 為最新的完整數據
        logging.debug(f"準備寫入 Google Sheet 的 DataFrame:\n{df}")
        write_current_seen_to_gsheet(df)
        sys.exit(0) # 沒有新商品，直接退出

    # 將當前爬取到的所有商品數據寫入 Google Sheets，以更新記錄
    logging.debug(f"準備寫入 Google Sheet 的 DataFrame:\n{df}")
    write_current_seen_to_gsheet(df)

    # 組合通知訊息
    notify_msg = "\n\n".join(notify_list)
    
    # 發送 LINE Broadcast 訊息
    if CHANNEL_ACCESS_TOKEN:
        logging.info("發送 LINE Broadcast 訊息")
        send_line_broadcast_message(notify_msg)
    else:
        logging.warning("LINE_CHANNEL_ACCESS_TOKEN 未設定，跳過 LINE 通知。")

    # 發送 GMAIL 通知
    if all([GMAIL_USER, GMAIL_TO, GMAIL_APP_PASSWORD]):
        logging.info("發送 GMAIL 通知")
        send_gmail("Hermès 新上架／變價通知", notify_msg)
    else:
        logging.warning("GMAIL 相關環境變數未完全設定，跳過 GMAIL 通知。")
        logging.warning(f"GMAIL_USER: {'已設定' if GMAIL_USER else '未設定'}")
        logging.warning(f"GMAIL_TO: {'已設定' if GMAIL_TO else '未設定'}")
        logging.warning(f"GMAIL_APP_PASSWORD: {'已設定' if GMAIL_APP_PASSWORD else '未設定'}")

    logging.info("只推播 Hermès 新品／變價商品（Broadcast + Google Sheets 記憶版）完成！")

if __name__ == "__main__":
    # 確保主程序在腳本直接執行時才運行
    main()
