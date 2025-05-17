import os
import json
import requests
import smtplib
import time
import traceback
from email.message import EmailMessage
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from linebot import LineBotApi
from linebot.models import TextSendMessage

# —— 狀態儲存 —— #
LAST_SEEN_FILE = "last_seen.json"

def load_last_seen():
    if os.path.exists(LAST_SEEN_FILE):
        return json.load(open(LAST_SEEN_FILE, encoding="utf-8"))
    return {}

def save_last_seen(data):
    json.dump(data, open(LAST_SEEN_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

# —— LINE Messaging API —— #
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")
if not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("❌ 未設定 LINE_CHANNEL_TOKEN 環境變數")
if not LINE_USER_ID:
    raise RuntimeError("❌ 未設定 LINE_USER_ID 環境變數")
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)

def send_line_message(text: str):
    try:
        # 印出 BOT 追蹤者數量
        followers = line_bot_api.get_number_of_followers()
        print(f"🔍 BOT 追蹤者數: {followers}")
        # 印出指定 USER_ID 的 Profile
        profile = line_bot_api.get_profile(LINE_USER_ID)
        print(f"👤 Profile: {profile.display_name} ({profile.user_id})")

        # 推播訊息
        line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=text))
        print("✅ 成功呼叫 LINE push_message")
    except Exception:
        print("❌ 發送 LINE push_message 失敗，詳細錯誤：")
        traceback.print_exc()

# —— Gmail SMTP —— #
def send_email_message(subject: str, body: str):
    user = os.getenv("GMAIL_USER")
    pwd  = os.getenv("GMAIL_PASS")
    to   = os.getenv("GMAIL_TO")
    if not user or not pwd or not to:
        print("❌ 請設定 GMAIL_USER, GMAIL_PASS, GMAIL_TO 環境變數")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = user
    msg["To"]      = to
    msg.set_content(body)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(user, pwd)
            server.send_message(msg)
        print("✅ 成功發送 Gmail 郵件")
    except Exception as e:
        print("❌ Gmail 發送失敗:", e)

# —— Selenium Driver —— #
def create_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.binary_location = "/usr/bin/chromium-browser"
    driver = webdriver.Chrome(options=options)
    driver.implicitly_wait(10)
    return driver

# —— Hermes 官網爬蟲 —— #
def scrape_hermes():
    url = "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"
    driver = create_driver()
    driver.get(url)
    for _ in range(6):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ul.product-list"))
        )
    except TimeoutException:
        print("⚠️ 等待 Hermès product-list 超時")
    items = driver.find_elements(By.CSS_SELECTOR, "ul.product-list li")[:5]
    results = []
    for item in items:
        try:
            name = item.find_element(
                By.CSS_SELECTOR, ".product-item-name, .product-name"
            ).text.strip()
            link = item.find_element(By.TAG_NAME, "a").get_attribute("href")
            price_nodes = item.find_elements(By.CSS_SELECTOR, ".price, .product-price")
            price = price_nodes[0].text.strip() if price_nodes else "無標價"
            results.append(f"👜 Hermès：{name}\\n💰 {price}\\n🔗 {link}")
        except Exception as e:
            print("解析錯誤（Hermès）：", e)
    driver.quit()
    return results

# —— 2nd STREET 爬蟲通用函式 —— #
def scrape_2nd_street(name, url):
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=10)
    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select("div.product-card") or soup.select(".product-item")
    results = []
    for card in cards[:5]:
        try:
            title = card.select_one(".product-name, .title").get_text(strip=True)
            link = card.select_one("a")["href"]
            if not link.startswith("http"):
                link = "https://store.2ndstreet.com.tw" + link
            price_tag = card.select_one(".price, .product-price")
            price = price_tag.get_text(strip=True) if price_tag else "無標價"
            results.append(f"🏪 {name}：{title}\\n💰 {price}\\n🔗 {link}")
        except Exception as e:
            print(f"解析錯誤（{name}）：", e)
    return results

# —— 單次任務邏輯（包含新舊比對） —— #
def job():
    print("⏰ 開始執行爬蟲與通知...")
    # 如果想測 LINE 推播，取消下面兩行註解可快速測試
    # send_line_message("【測試】程式已啟動！")
    # return

    last = load_last_seen()
    notify = []
    new_seen = {}

    # Hermes
    hermes = scrape_hermes()
    if hermes:
        first_link = hermes[0].split("\\n🔗 ")[1]
        if last.get("hermes") != first_link:
            notify += hermes
            new_seen["hermes"] = first_link

    # 2nd STREET HERMES
    s2_hermes = scrape_2nd_street(
        "2nd STREET HERMES",
        "https://store.2ndstreet.com.tw/v2/Search?q=HERMES&shopId=41320&order=Newest"
    )
    if s2_hermes:
        link2 = s2_hermes[0].split("\\n🔗 ")[1]
        if last.get("s2_hermes") != link2:
            notify += s2_hermes
            new_seen["s2_hermes"] = link2

    # CHANEL
    s2_chanel = scrape_2nd_street(
        "2nd STREET CHANEL",
        "https://store.2ndstreet.com.tw/v2/Search?q=CHANEL&shopId=41320&order=Newest"
    )
    if s2_chanel:
        link3 = s2_chanel[0].split("\\n🔗 ")[1]
        if last.get("s2_chanel") != link3:
            notify += s2_chanel
            new_seen["s2_chanel"] = link3

    # Dior
    s2_dior = scrape_2nd_street(
        "2nd STREET Dior",
        "https://store.2ndstreet.com.tw/v2/Search?q=Christian+Dior&shopId=41320&order=Newest"
    )
    if s2_dior:
        link4 = s2_dior[0].split("\\n🔗 ")[1]
        if last.get("s2_dior") != link4:
            notify += s2_dior
            new_seen["s2_dior"] = link4

    # 發送通知
    if notify:
        header = f"📦 共偵測到 {len(notify)} 件新品，前 5 筆：\\n\\n"
        body = "\\n\\n".join(notify[:5])
        send_line_message(header + body)
        send_email_message("新品上架通知", header + body)
        last.update(new_seen)
        save_last_seen(last)
    else:
        print("👍 沒有檢測到新商品")

if __name__ == "__main__":
    job()
