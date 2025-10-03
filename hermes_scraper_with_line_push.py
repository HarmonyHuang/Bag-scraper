import os
import time
import re
import json
import unicodedata
from datetime import datetime
from urllib.parse import urlparse

import requests
import pandas as pd
import gspread
import yagmail

from google.oauth2.service_account import Credentials
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==== 常數 ====
HTTP_TIMEOUT = 30
LINE_MAX_BYTES = 4900  # 安全邊界 (< 5000 bytes)
GSHEETS_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# ==== 全域快取 ====
_REQ_SESSION: requests.Session | None = None
_GS_CLIENT: gspread.Client | None = None

def _get_session() -> requests.Session:
    """帶自動重試的 requests.Session（LINE/Gmail 用得到）。"""
    global _REQ_SESSION
    if _REQ_SESSION:
        return _REQ_SESSION
    s = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"]),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    _REQ_SESSION = s
    return s

# ==== 環境變數 (GitHub Secrets 或本地 Export) ====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GMAIL_USER           = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD   = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO             = os.getenv("GMAIL_TO", "")
GSHEET_ID            = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON    = os.getenv("GOOGLE_CREDS_JSON", "")

# ===== Hermès 官方網站 要爬的兩個分類 =====
hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件",     "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

# ===================== 通知：LINE / Gmail =====================

def _chunk_by_bytes(s: str, max_bytes: int = LINE_MAX_BYTES) -> list[str]:
    """依 UTF-8 位元組長度分段，避免超過 LINE 單則限制。"""
    out, buf = [], bytearray()
    for ch in s:
        b = ch.encode("utf-8")
        if len(buf) + len(b) > max_bytes:
            out.append(buf.decode("utf-8", errors="ignore"))
            buf = bytearray(b)
        else:
            buf.extend(b)
    if buf:
        out.append(buf.decode("utf-8", errors="ignore"))
    return out if out else [""]

def send_line_broadcast_message(text: str) -> bool:
    """
    以 broadcast 方式推播給所有追蹤者。
    內建【UTF-8 位元組】分段，避免超過 LINE 單次上限。
    """
    if not CHANNEL_ACCESS_TOKEN:
        print("未設定 LINE_CHANNEL_ACCESS_TOKEN，略過 LINE 推播。")
        return True

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    sess = _get_session()

    try:
        parts = _chunk_by_bytes(text, LINE_MAX_BYTES)
        ok_all = True
        for idx, part in enumerate(parts, 1):
            payload = {"messages": [{"type": "text", "text": part}]}
            r = sess.post(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
            print(f"LINE Broadcast ({idx}/{len(parts)}): {r.status_code} {r.text}")
            ok_all = ok_all and r.ok
            time.sleep(0.4)  # 避免節流
        return ok_all
    except Exception as e:
        print("LINE Broadcast 失敗：", e)
        return False

def send_gmail(subject: str, body: str) -> bool:
    """用 yagmail 寄送；未設帳號時略過返回 True。"""
    if not GMAIL_USER:
        print("未設定 GMAIL_USER，略過 Gmail 推播。")
        return True
    try:
        yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
        to_list = [addr.strip() for addr in (GMAIL_TO or "").split(",") if addr.strip()]
        if not to_list:
            print("未設定 GMAIL_TO，略過 Gmail 推播。")
            return True
        yag.send(to=to_list, subject=(subject or "(no subject)"), contents=body or "")
        print("Gmail 寄送完成")
        return True
    except Exception as e:
        print("Gmail 寄送失敗：", e)
        return False

# ===================== Google Sheets 工具 =====================

def get_gsheet_client() -> gspread.Client:
    """
    從環境變數 GOOGLE_CREDS_JSON 讀取 Service Account JSON，
    授權並返回 gspread client（有快取）。
    """
    global _GS_CLIENT
    if _GS_CLIENT:
        return _GS_CLIENT
    if not GOOGLE_CREDS_JSON.strip():
        raise RuntimeError("GOOGLE_CREDS_JSON 未提供，無法連線 Google Sheets。")
    print("進入 get_gsheet_client()")
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=GSHEETS_SCOPE)
    _GS_CLIENT = gspread.authorize(creds)
    print("Google Sheets 驗證成功")
    return _GS_CLIENT

def write_current_seen_to_gsheet(df: pd.DataFrame):
    """
    將「當前快照」寫入 Sheet1（供檢視）。
    先清空再更新，欄數依 df 動態計算，不再寫死到 F 欄。
    """
    print("==== 準備寫入 Google Sheets Sheet1（快照） ====")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        try:
            ws = sh.worksheet("Sheet1")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="Sheet1", rows="1", cols=str(max(6, len(df.columns) + 2)))

        all_values = [df.columns.astype(str).tolist()] + df.fillna("").astype(str).values.tolist()
        ws.clear()
        # 依據列數/欄數動態決定範圍
        rows = len(all_values)
        cols = len(all_values[0]) if all_values else 1
        end_col_letter = gspread.utils.rowcol_to_a1(1, cols).split("1")[0]  # 例如 'F'
        ws.update(range_name=f"A1:{end_col_letter}{rows}", values=all_values)
        print("==== 已寫入 Sheet1（快照） ====")
    except Exception as e:
        print("寫入 Sheet1 失敗：", e)

def _ensure_seen_sheet(sh) -> gspread.Worksheet:
    """確保存在 Seen 工作表，沒有就建立標題列。"""
    try:
        ws = sh.worksheet("Seen")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Seen", rows="1", cols="4")
        ws.update(values=[["id_key", "last_price", "first_seen_at", "last_updated_at"]], range_name="A1:D1")
    return ws

def get_seen_price_map() -> dict[str, str]:
    """
    從 Seen 讀出「最新」的 id_key -> last_price 映射。
    若歷史有多筆相同 id_key，取最後一筆為準（覆蓋）。
    """
    client = get_gsheet_client()
    sh = client.open_by_key(GSHEET_ID)
    ws = _ensure_seen_sheet(sh)
    rows = ws.get_all_values()
    m: dict[str, str] = {}
    if len(rows) > 1:
        for r in rows[1:]:
            if not r or not r[0]:
                continue
            id_key = r[0]
            last_price = r[1] if len(r) > 1 else ""
            m[id_key] = last_price
    print(f"Seen 映射載入完成：{len(m)} 筆")
    return m

def append_seen_prices(pairs: list[tuple[str, str]]) -> bool:
    """
    將 (id_key, price_norm) 追加到 Seen。
    - 新商品：first_seen_at = now, last_updated_at = now
    - 價格變動：直接再追加一行（讀取時會自動以最後一筆為準）
    """
    if not pairs:
        return True
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        ws = _ensure_seen_sheet(sh)
        now = datetime.now().isoformat(timespec="seconds")
        values = [[k, p, now, now] for (k, p) in pairs]
        # gspread 新版支援 append_rows；舊版可改用 ws.append_row 迴圈
        ws.append_rows(values)
        print(f"Seen 追加完成：{len(values)} 筆")
        return True
    except Exception as e:
        print("追加 Seen 失敗：", e)
        return False

# ===================== 正規化與唯一鍵 =====================

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
    # 只取數字：NT$ 12,300 -> "12300"
    return "".join(ch for ch in p if ch.isdigit())

def canonical_link(link: str) -> str:
    """
    規範化 URL：小寫網域、去掉查詢字串與 fragment、去尾斜線。
    相對路徑轉絕對（預設 hermes.com）。
    """
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
    """從 Hermès 產品連結擷取 PID（形如 …-p-XXXX）。"""
    if not link:
        return ""
    try:
        path = urlparse(link).path.lower()
        m = re.search(r"-p-([a-z0-9]+)", path)
        return m.group(1) if m else ""
    except Exception:
        return ""

def make_id_key(link: str, name: str, color: str) -> str:
    """
    以 PID 作為唯一識別；若缺少 PID，退回 URL；再退回文字 key。
    """
    pid = product_pid_from_link(link)
    if pid:
        return f"pid:{pid}"
    url_key = canonical_link(link)
    if url_key:
        return f"url:{url_key}"
    return f"text:{normalize_text(name)}|{normalize_text(color)}"

# ===================== 爬取 Hermes =====================

def scrape_hermes() -> pd.DataFrame:
    hermes_data: list[dict] = []

    chrome_options = Options()
    # GitHub Actions 需要 headless
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options
    )

    try:
        for cname, url in hermes_urls:
            print(f"抓取 Hermès 分類: {cname} -> {url}")
            driver.get(url)
            time.sleep(5)  # 簡易等待；要更穩可改 WebDriverWait/滾動

            # 包容多種列表容器
            items = driver.find_elements(
                By.CSS_SELECTOR,
                "div.product-grid-item, div.product-grid-list-item, li.product-grid-item"
            )
            print(f"► 本次共抓到 {len(items)} 件 Hermès 「{cname}」")

            for it in items:
                name = color = price = link = img = ""

                # 名稱與連結
                try:
                    name_el = it.find_element(By.CSS_SELECTOR, ".product-item-name, a.product-item-name")
                    link = name_el.get_attribute("href") or ""
                    if link.startswith("/"):
                        link = "https://www.hermes.com" + link
                    # 有些情況文字在 <span>
                    name = (name_el.text or "").strip()
                    if not name:
                        try:
                            name = name_el.find_element(By.CSS_SELECTOR, "span").text.strip()
                        except Exception:
                            pass
                except NoSuchElementException:
                    pass

                # 顏色（修正 getText → text）
                try:
                    color = it.find_element(By.CSS_SELECTOR, ".product-item-colors").text.strip()
                except Exception:
                    color = ""

                # 價格（多種標記）
                for sel in (".price", "[itemprop='price']", "span.price"):
                    if price:
                        break
                    try:
                        price = it.find_element(By.CSS_SELECTOR, sel).text.strip()
                    except Exception:
                        pass

                # 圖片
                try:
                    raw_src = it.find_element(By.CSS_SELECTOR, "img").get_attribute("src") or ""
                    img = ("https:" + raw_src) if raw_src.startswith("//") else raw_src
                except Exception:
                    img = ""

                hermes_data.append({
                    "source": f"Hermès官網 {cname}",
                    "name":   name,
                    "color":  color,
                    "price":  price,
                    "link":   link,
                    "img":    img,
                })
    finally:
        driver.quit()

    df = pd.DataFrame(hermes_data, columns=["source", "name", "color", "price", "link", "img"])
    return df

# ===================== 主流程 =====================

def main():
    # 1) 抓資料
    df = scrape_hermes()

    # 2) 讀取已推播記憶（id_key -> last_price）
    seen_prices = get_seen_price_map()

    # 3) 比對：新貨或變價才通知；同一輪去重
    notify_list = []
    to_append_pairs = []   # 要寫入 Seen 的 (id_key, price_n)
    seen_in_run: set[tuple[str, str]] = set()

    for _, row in df.iterrows():
        name  = row.get("name", "")
        color = row.get("color", "")
        price = row.get("price", "")
        link  = row.get("link", "")

        id_key  = make_id_key(link, name, color)
        price_n = normalize_price(price)

        # 單輪去重：同一商品同一價格，當輪只處理一次
        run_key = (id_key, price_n)
        if run_key in seen_in_run:
            continue
        seen_in_run.add(run_key)

        prev_price = seen_prices.get(id_key)  # 可能為 None/""/"12300"
        is_new_product  = (prev_price is None)  # 從未出現過
        is_price_change = (prev_price is not None and price_n and price_n != prev_price)

        if is_new_product or is_price_change:
            notify_list.append(
                f"[{row['source']}]\n{row['name']} {row.get('color','')} {row['price']}\n{row['link']}"
            )
            to_append_pairs.append((id_key, price_n if price_n else ""))  # 記錄目前價格（可為空）
            # 先更新本地映射，避免同輪同商品再次進入
            seen_prices[id_key] = price_n

    # 4) 更新快照（給人看），與記憶（給機器判斷）
    write_current_seen_to_gsheet(df)

    if not notify_list:
        print("本次無新增或變價商品，跳過通知。")
        return

    # 先寫入「記憶」，成功才推播，避免下輪重複
    if append_seen_prices(to_append_pairs):
        notify_msg = "\n\n".join(notify_list)
        ok_line  = send_line_broadcast_message(notify_msg)
        ok_gmail = send_gmail("Hermès 新上架／變價通知", notify_msg)
        print(f"推播完成（LINE={ok_line}, GMAIL={ok_gmail}）")
    else:
        print("❗ 追加 Seen 失敗，為避免重複推播，本輪不發送通知。")


if __name__ == "__main__":
    main()
