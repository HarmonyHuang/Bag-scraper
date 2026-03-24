import os
import time
import re
import json
import logging
import unicodedata
from datetime import datetime, time as dtime
from urllib.parse import urlparse
from typing import List, Dict, Tuple, Optional

import requests
import pandas as pd
import gspread
import yagmail
from google.oauth2.service_account import Credentials
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo

# ==== 配置與日誌 ====
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30
LINE_MAX_BYTES = 4900
GSHEETS_SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
RUN_WINDOW_START = dtime(6, 0)
RUN_WINDOW_END = dtime(23, 59, 59)

# ==== 全域快取 ====
_REQ_SESSION: Optional[requests.Session] = None
_GS_CLIENT: Optional[gspread.Client] = None

# ==== 環境變數 ====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO = os.getenv("GMAIL_TO", "")
GSHEET_ID = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")

HERMES_URLS = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

# ===================== 工具函數 =====================

def _get_session() -> requests.Session:
    global _REQ_SESSION
    if _REQ_SESSION: return _REQ_SESSION
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.8, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    _REQ_SESSION = s
    return s

def _chunk_by_bytes(s: str, max_bytes: int = LINE_MAX_BYTES) -> List[str]:
    out, buf = [], bytearray()
    for ch in s:
        b = ch.encode("utf-8")
        if len(buf) + len(b) > max_bytes:
            out.append(buf.decode("utf-8", errors="ignore"))
            buf = bytearray(b)
        else:
            buf.extend(b)
    if buf: out.append(buf.decode("utf-8", errors="ignore"))
    return out

# ===================== 通知系統 =====================

def send_line_broadcast(text: str) -> bool:
    if not CHANNEL_ACCESS_TOKEN: return True
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"}
    sess = _get_session()
    try:
        parts = _chunk_by_bytes(text)
        for part in parts:
            sess.post(url, headers=headers, json={"messages": [{"type": "text", "text": part}]}, timeout=HTTP_TIMEOUT)
            time.sleep(0.5)
        return True
    except Exception as e:
        logger.error(f"LINE 失敗: {e}")
        return False

def send_gmail_notification(subject: str, body: str) -> bool:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and GMAIL_TO): return True
    try:
        yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
        to_list = [addr.strip() for addr in GMAIL_TO.split(",") if addr.strip()]
        yag.send(to=to_list, subject=subject, contents=body)
        logger.info("Gmail 已發送")
        return True
    except Exception as e:
        logger.error(f"Gmail 失敗: {e}")
        return False

# ===================== Google Sheets 邏輯 =====================

def get_gsheet_client() -> gspread.Client:
    global _GS_CLIENT
    if _GS_CLIENT: return _GS_CLIENT
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDS_JSON), scopes=GSHEETS_SCOPE)
    _GS_CLIENT = gspread.authorize(creds)
    return _GS_CLIENT

def update_gsheet_snapshot(df: pd.DataFrame):
    """更新當前網頁看到的商品快照 (Sheet1)"""
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        try:
            ws = sh.worksheet("Sheet1")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet("Sheet1", rows=100, cols=10)
        
        data = [df.columns.tolist()] + df.fillna("").values.tolist()
        ws.clear()
        ws.update(data) # gspread 3.7+ 會自動處理範圍
        logger.info("Sheet1 快照更新完成")
    except Exception as e:
        logger.error(f"快照更新失敗: {e}")

def get_seen_map() -> Dict[str, str]:
    """取得歷史價格記錄 (Seen 表)"""
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        try:
            ws = sh.worksheet("Seen")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet("Seen", rows=1, cols=4)
            ws.append_row(["id_key", "last_price", "first_seen", "last_updated"])
            return {}
        
        records = ws.get_all_records()
        return {str(r['id_key']): str(r['last_price']) for r in records if r.get('id_key')}
    except Exception as e:
        logger.error(f"讀取 Seen 失敗: {e}")
        return {}

def record_new_seen(pairs: List[Tuple[str, str]]):
    """記錄新發現或變價商品"""
    if not pairs: return
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = sh.worksheet("Seen")
        now = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")
        rows = [[p[0], p[1], now, now] for p in pairs]
        ws.append_rows(rows)
    except Exception as e:
        logger.error(f"追加記錄失敗: {e}")

# ===================== 網頁解析與爬蟲 =====================

def normalize_price(p: str) -> str:
    return "".join(filter(str.isdigit, p))

def make_id_key(link: str, name: str) -> str:
    # 優先從網址提取唯一商品編號
    pid_match = re.search(r"-([A-Z0-9]+)/?$", link)
    if pid_match: return f"pid:{pid_match.group(1)}"
    return f"name:{name.strip()}"

def _parse_product_item(it, source_name: str) -> Optional[Dict]:
    """解析單個商品元件"""
    try:
        # 抓取名稱與連結
        name_el = it.find_element(By.CSS_SELECTOR, "a.product-item-name")
        name = name_el.text.strip()
        link = name_el.get_attribute("href")
        
        # 抓取價格 (針對之前提供的 h-price 結構優化)
        price = ""
        try:
            price_el = it.find_element(By.CSS_SELECTOR, ".price, h-price .price")
            price = price_el.text.strip()
        except NoSuchElementException:
            pass
            
        # 抓取顏色
        color = ""
        try:
            color_el = it.find_element(By.CSS_SELECTOR, ".product-item-colors")
            color = color_el.text.replace("顏色:", "").strip()
        except NoSuchElementException:
            pass

        return {
            "source": source_name,
            "name": name,
            "color": color,
            "price": price,
            "link": link,
            "id_key": make_id_key(link, name)
        }
    except Exception:
        return None

def scrape_hermes() -> pd.DataFrame:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

    results = []
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    
    try:
        for cname, url in HERMES_URLS:
            logger.info(f"正在爬取: {cname}")
            driver.get(url)
            
            # 等待清單加載
            try:
                WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".product-grid-item, .product-grid-list-item")))
            except TimeoutException:
                logger.warning(f"{cname} 頁面加載逾時")
                continue

            # 滾動加載更多 (瀑布流)
            for _ in range(5): 
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1)

            items = driver.find_elements(By.CSS_SELECTOR, ".product-grid-item, .product-grid-list-item")
            for it in items:
                p_data = _parse_product_item(it, f"Hermès {cname}")
                if p_data: results.append(p_data)
                
    finally:
        driver.quit()
    
    return pd.DataFrame(results)

# ===================== 主程式 =====================

def main():
    # 檢查執行視窗
    now_t = datetime.now(TAIPEI_TZ).time()
    if not (RUN_WINDOW_START <= now_t <= RUN_WINDOW_END):
        logger.info("非台北執行時段，跳過。")
        return

    # 1. 爬取
    df = scrape_hermes()
    if df.empty:
        logger.warning("本次未抓取到任何資料。")
        return

    # 2. 判斷與比對
    seen_map = get_seen_map()
    notify_list = []
    new_pairs = []

    for _, row in df.iterrows():
        id_key = row['id_key']
        current_p_norm = normalize_price(row['price'])
        prev_p_norm = seen_map.get(id_key)

        # 比對邏輯：未曾見過 OR 價格有變
        is_new = (prev_p_norm is None)
        is_changed = (prev_p_norm is not None and current_p_norm and current_p_norm != prev_p_norm)

        if is_new or is_changed:
            status = "✨ 新上架" if is_new else "🔔 價格變動"
            msg = f"[{status} - {row['source']}]\n{row['name']}\n顏色: {row['color']}\n價格: {row['price']}\n{row['link']}"
            notify_list.append(msg)
            new_pairs.append((id_key, current_p_norm))
            # 更新 map 避免同次重複
            seen_map[id_key] = current_p_norm

    # 3. 寫入快照
    update_gsheet_snapshot(df.drop(columns=['id_key']))

    # 4. 發送通知
    if notify_list:
        combined_msg = "\n" + ("="*20) + "\n\n" + "\n\n".join(notify_list)
        # 先存記錄，再發通知，防重複
        record_new_seen(new_pairs)
        send_line_broadcast(combined_msg)
        send_gmail_notification("Hermès 官網發現新品/變價", combined_msg)
        logger.info(f"通知發送完成: {len(notify_list)} 筆")
    else:
        logger.info("無更新，不發送通知。")

if __name__ == "__main__":
    main()
