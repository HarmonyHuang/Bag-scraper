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
    ("小皮件",      "
