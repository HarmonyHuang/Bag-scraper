import os
import time
import re
import json
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
LINE_MAX_BYTES = 4900  # 安全邊界 (< 5000 bytes)
GSHEETS_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
TAIPEI_TZ = "Asia/Taipei"
RUN_WINDOW_START = dtime(6, 0)
RUN_WINDOW_END   = dtime(23, 59)

# ==== 全域快取 ====
_REQ_SESSION: Optional[requests.Session] = None
_GS_CLIENT: Optional[gspread.Client] = None

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

def _chunk_by_bytes(s: str, max_bytes: int = LINE_MAX_BYTES) -> List[str]:
    """依 UTF-8 位元組長度分段，避免超過 LINE 單則限制。"""
    out, buf = [], bytearray()
    for ch in s:
        b = ch.encode("utf-8")
        if len(buf) + len(b) > max_bytes:
            ou
