import os
import time
import re
import json
import unicodedata
from datetime import datetime, time as dtime
from urllib.parse import urlparse
from typing import List, Dict, Tuple, Optional
from zoneinfo import ZoneInfo

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
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==== 常數 ====
HTTP_TIMEOUT = 30
LINE_MAX_BYTES = 4900
GSHEETS_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
RUN_WINDOW_START = dtime(6, 0, 0)
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

# ===== Hermès 官方網站 要爬的兩個分類 =====
HERMES_URLS = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

# ===================== 共用工具 =====================

def now_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)

def is_within_taipei_window() -> bool:
    t = now_taipei().time()
    return RUN_WINDOW_START <= t <= RUN_WINDOW_END

def _get_session() -> requests.Session:
    global _REQ_SESSION
    if _REQ_SESSION:
        return _REQ_SESSION

    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    _REQ_SESSION = session
    return session

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("顏色:", "").replace("顏色：", "")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

def normalize_price(p: str) -> str:
    if not p:
        return ""
    return "".join(ch for ch in p if ch.isdigit())

def canonical_link(link: str) -> str:
    if not link:
        return ""
    try:
        if link.startswith("//"):
            link = "https:" + link
        if link.startswith("/"):
            link = "https://www.hermes.com" + link
        u = urlparse(link)
        scheme = "https"
        netloc = u.netloc.lower()
        path = u.path.rstrip("/")
        return f"{scheme}://{netloc}{path}"
    except Exception:
        return link

def product_pid_from_link(link: str) -> str:
    if not link:
        return ""
    try:
        path = urlparse(link).path.lower()
        m = re.search(r"-p-([a-z0-9]+)", path)
        return m.group(1) if m else ""
    except Exception:
        return ""

def make_id_key(link: str, name: str, color: str) -> str:
    pid = product_pid_from_link(link)
    if pid:
        return f"pid:{pid}"

    url_key = canonical_link(link)
    if url_key:
        return f"url:{url_key}"

    return f"text:{normalize_text(name)}|{normalize_text(color)}"

# ===================== 通知：LINE / Gmail =====================

def _chunk_by_bytes(text: str, max_bytes: int = LINE_MAX_BYTES) -> List[str]:
    chunks: List[str] = []
    buf = bytearray()

    for ch in text:
        b = ch.encode("utf-8")
        if len(buf) + len(b) > max_bytes:
            chunks.append(buf.decode("utf-8", errors="ignore"))
            buf = bytearray(b)
        else:
            buf.extend(b)

    if buf:
        chunks.append(buf.decode("utf-8", errors="ignore"))

    return chunks if chunks else [""]

def send_line_broadcast_message(text: str) -> bool:
    if not CHANNEL_ACCESS_TOKEN:
        print("未設定 LINE_CHANNEL_ACCESS_TOKEN，略過 LINE 推播。")
        return True

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    session = _get_session()

    try:
        parts = _chunk_by_bytes(text, LINE_MAX_BYTES)
        ok_all = True

        for idx, part in enumerate(parts, start=1):
            payload = {"messages": [{"type": "text", "text": part}]}
            r = session.post(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
            print(f"LINE Broadcast ({idx}/{len(parts)}): {r.status_code} {r.text}")
            ok_all = ok_all and r.ok
            time.sleep(0.4)

        return ok_all
    except Exception as e:
        print("LINE Broadcast 失敗：", repr(e))
        return False

def send_gmail(subject: str, body: str) -> bool:
    if not GMAIL_USER:
        print("未設定 GMAIL_USER，略過 Gmail 推播。")
        return True

    try:
        to_list = [x.strip() for x in (GMAIL_TO or "").split(",") if x.strip()]
        if not to_list:
            print("未設定 GMAIL_TO，略過 Gmail 推播。")
            return True

        yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
        yag.send(to=to_list, subject=subject or "(no subject)", contents=body or "")
        print("Gmail 寄送完成")
        return True
    except Exception as e:
        print("Gmail 寄送失敗：", repr(e))
        return False

# ===================== Google Sheets 工具 =====================

def get_gsheet_client() -> gspread.Client:
    global _GS_CLIENT

    if _GS_CLIENT:
        return _GS_CLIENT

    if not GOOGLE_CREDS_JSON.strip():
        raise RuntimeError("GOOGLE_CREDS_JSON 未提供，無法連線 Google Sheets。")

    print("進入 get_gsheet_client()")

    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
    except Exception as e:
        raise RuntimeError(f"GOOGLE_CREDS_JSON 不是合法 JSON: {repr(e)}") from e

    creds = Credentials.from_service_account_info(creds_dict, scopes=GSHEETS_SCOPE)
    _GS_CLIENT = gspread.authorize(creds)

    print("Google Sheets 驗證成功")
    return _GS_CLIENT

def _ensure_sheet1(sh) -> gspread.Worksheet:
    try:
        ws = sh.worksheet("Sheet1")
        print("找到 Sheet1")
    except gspread.WorksheetNotFound:
        print("找不到 Sheet1，建立新的 Sheet1")
        ws = sh.add_worksheet(title="Sheet1", rows="100", cols="10")
    return ws

def _ensure_seen_sheet(sh) -> gspread.Worksheet:
    try:
        ws = sh.worksheet("Seen")
        print("找到 Seen")
    except gspread.WorksheetNotFound:
        print("找不到 Seen，建立新的 Seen")
        ws = sh.add_worksheet(title="Seen", rows="100", cols="10")
        ws.update(
            range_name="A1:D1",
            values=[["id_key", "last_price", "first_seen_at", "last_updated_at"]]
        )
    return ws

def write_current_seen_to_gsheet(df: pd.DataFrame):
    print("==== 準備寫入 Google Sheets Sheet1（快照） ====")
    print(f"df 筆數: {len(df)}")
    print(f"df 欄位: {list(df.columns)}")

    try:
        client = get_gsheet_client()
        print("open_by_key 前，GSHEET_ID =", GSHEET_ID)

        sh = client.open_by_key(GSHEET_ID)
        print("成功打開 spreadsheet")

        ws = _ensure_sheet1(sh)

        if df.empty:
            all_values = [["source", "name", "color", "price", "link", "img"]]
        else:
            all_values = [df.columns.astype(str).tolist()] + df.fillna("").astype(str).values.tolist()

        rows = len(all_values)
        cols = len(all_values[0]) if all_values else 1
        end_cell = gspread.utils.rowcol_to_a1(rows, cols)

        ws.clear()
        ws.update(range_name=f"A1:{end_cell}", values=all_values)

        print("==== 已寫入 Sheet1（快照） ====")
    except Exception as e:
        print("寫入 Sheet1 失敗：", repr(e))

def get_seen_price_map() -> Dict[str, str]:
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = _ensure_seen_sheet(sh)

        rows = ws.get_all_values()
        result: Dict[str, str] = {}

        if len(rows) > 1:
            for r in rows[1:]:
                if not r or not r[0]:
                    continue
                id_key = r[0]
                last_price = r[1] if len(r) > 1 else ""
                result[id_key] = last_price

        print(f"Seen 映射載入完成：{len(result)} 筆")
        return result

    except Exception as e:
        print("讀取 Seen 失敗：", repr(e))
        return {}

def append_seen_prices(pairs: List[Tuple[str, str]]) -> bool:
    if not pairs:
        return True

    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = _ensure_seen_sheet(sh)

        now_str = now_taipei().isoformat(timespec="seconds")
        values = [[k, p, now_str, now_str] for (k, p) in pairs]

        ws.append_rows(values)
        print(f"Seen 追加完成：{len(values)} 筆")
        return True

    except Exception as e:
        print("追加 Seen 失敗：", repr(e))
        return False

# ===================== Selenium 幫手 =====================

def _wait_for_item_list(driver, timeout: int = 20) -> None:
    selector = "div.product-grid-item, div.product-grid-list-item, li.product-grid-item"
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
    )

def _scroll_to_load_all(driver, max_rounds: int = 20, pause: float = 0.8) -> None:
    selector = "div.product-grid-item, div.product-grid-list-item, li.product-grid-item"
    last_count = -1

    for _ in range(max_rounds):
        items = driver.find_elements(By.CSS_SELECTOR, selector)
        count = len(items)

        if count == last_count:
            break

        last_count = count
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(pause)

# ===================== 爬取 Hermes =====================

def scrape_hermes() -> pd.DataFrame:
    hermes_data: List[Dict] = []

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1366,1000")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    driver = None

    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=chrome_options
        )

        for cname, url in HERMES_URLS:
            print(f"抓取 Hermès 分類: {cname} -> {url}")
            driver.get(url)

            try:
                _wait_for_item_list(driver, timeout=20)
            except TimeoutException:
                print("⚠️ 等待商品清單逾時，改為固定等待 5 秒")
                time.sleep(5)

            _scroll_to_load_all(driver)

            selector = "div.product-grid-item, div.product-grid-list-item, li.product-grid-item"
            items = driver.find_elements(By.CSS_SELECTOR, selector)
            print(f"► 本次共抓到 {len(items)} 件 Hermès 「{cname}」")

            for it in items:
                name = color = price = link = img = ""

                try:
                    name_el = it.find_element(By.CSS_SELECTOR, ".product-item-name, a.product-item-name")
                    link = name_el.get_attribute("href") or ""
                    if link.startswith("/"):
                        link = "https://www.hermes.com" + link

                    name = (name_el.text or "").strip()
                    if not name:
                        try:
                            name = name_el.find_element(By.CSS_SELECTOR, "span").text.strip()
                        except Exception:
                            pass

                except NoSuchElementException:
                    pass
                except StaleElementReferenceException:
                    try:
                        name = it.text.strip()
                    except Exception:
                        pass

                try:
                    color = it.find_element(By.CSS_SELECTOR, ".product-item-colors").text.strip()
                except Exception:
                    color = ""

                for sel_price in (".price", "[itemprop='price']", "span.price"):
                    if price:
                        break
                    try:
                        price = it.find_element(By.CSS_SELECTOR, sel_price).text.strip()
                    except Exception:
                        pass

                try:
                    raw_src = it.find_element(By.CSS_SELECTOR, "img").get_attribute("src") or ""
                    img = ("https:" + raw_src) if raw_src.startswith("//") else raw_src
                except Exception:
                    img = ""

                hermes_data.append({
                    "source": f"Hermès官網 {cname}",
                    "name": name,
                    "color": color,
                    "price": price,
                    "link": link,
                    "img": img,
                })

    except WebDriverException as e:
        print("Chrome / WebDriver 啟動失敗：", repr(e))

    finally:
        if driver:
            driver.quit()

    df = pd.DataFrame(hermes_data, columns=["source", "name", "color", "price", "link", "img"])
    print(f"本次抓到資料筆數: {len(df)}")
    print(df.head())
    return df

# ===================== 主流程 =====================

def main():
    if not is_within_taipei_window():
        print("目前非台北時間 06:00–24:00，跳過此次執行（不爬、不推播、不寫表）。")
        return

    start_ts = time.time()
    print("=== Hermes scraper 開始執行 ===")
    print("台北現在時間：", now_taipei().isoformat(timespec="seconds"))

    # 1) 抓資料
    df = scrape_hermes()

    # 不管有沒有資料，都先寫入 Sheet1
    write_current_seen_to_gsheet(df)

    if df.empty:
        print("⚠️ 本次爬到 0 筆資料，不推播。")
        print(f"總耗時：{time.time() - start_ts:.1f}s")
        return

    # 2) 讀取已推播記憶
    seen_prices = get_seen_price_map()

    # 3) 比對：新貨或變價才通知；同一輪去重
    notify_list: List[str] = []
    to_append_pairs: List[Tuple[str, str]] = []
    seen_in_run: set[Tuple[str, str]] = set()

    for _, row in df.iterrows():
        name = row.get("name", "")
        color = row.get("color", "")
        price = row.get("price", "")
        link = row.get("link", "")

        id_key = make_id_key(link, name, color)
        price_n = normalize_price(price)

        run_key = (id_key, price_n)
        if run_key in seen_in_run:
            continue
        seen_in_run.add(run_key)

        prev_price = seen_prices.get(id_key)
        is_new_product = (prev_price is None)
        is_price_change = (prev_price is not None and price_n and price_n != prev_price)

        if is_new_product or is_price_change:
            notify_list.append(
                f"[{row['source']}]\n{row['name']} {row.get('color', '')} {row['price']}\n{row['link']}"
            )
            to_append_pairs.append((id_key, price_n if price_n else ""))
            seen_prices[id_key] = price_n

    if not notify_list:
        print("本次無新增或變價商品，跳過通知。")
        print(f"總耗時：{time.time() - start_ts:.1f}s")
        return

    if append_seen_prices(to_append_pairs):
        notify_msg = "\n\n".join(notify_list)
        ok_line = send_line_broadcast_message(notify_msg)
        ok_gmail = send_gmail("Hermès 新上架／變價通知", notify_msg)
        print(f"推播完成（LINE={ok_line}, GMAIL={ok_gmail}）")
    else:
        print("❗ 追加 Seen 失敗，為避免重複推播，本輪不發送通知。")

    print(f"總耗時：{time.time() - start_ts:.1f}s")

if __name__ == "__main__":
    main()
