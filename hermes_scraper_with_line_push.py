import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

GSHEET_ID = os.getenv("GSHEET_ID", "")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")

GSHEETS_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def get_gsheet_client():
    if not GOOGLE_CREDS_JSON.strip():
        raise RuntimeError("GOOGLE_CREDS_JSON 沒有設定")

    print("開始解析 GOOGLE_CREDS_JSON")
    creds_dict = json.loads(GOOGLE_CREDS_JSON)

    print("Service account email =", creds_dict.get("client_email", "N/A"))

    creds = Credentials.from_service_account_info(creds_dict, scopes=GSHEETS_SCOPE)
    client = gspread.authorize(creds)
    print("Google Sheets 驗證成功")
    return client


def main():
    print("=== 開始測試寫入 Google Sheet ===")
    print("GSHEET_ID =", GSHEET_ID)

    if not GSHEET_ID:
        raise RuntimeError("GSHEET_ID 沒有設定")

    client = get_gsheet_client()

    print("準備 open_by_key")
    sh = client.open_by_key(GSHEET_ID)
    print("成功打開 spreadsheet，標題 =", sh.title)

    try:
        ws = sh.worksheet("Sheet1")
        print("找到既有 Sheet1")
    except gspread.WorksheetNotFound:
        print("找不到 Sheet1，建立新的")
        ws = sh.add_worksheet(title="Sheet1", rows="100", cols="10")

    now_str = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")

    values = [
        ["status", "time", "message"],
        ["OK", now_str, "這是一筆測試資料"],
    ]

    print("準備清空並寫入 Sheet1")
    ws.clear()
    ws.update(range_name="A1:C2", values=values)

    print("=== 已成功寫入 Google Sheet ===")


if __name__ == "__main__":
    main()
