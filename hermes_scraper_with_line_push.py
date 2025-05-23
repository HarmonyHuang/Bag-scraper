import os
import json
import time
import traceback
from email.message import EmailMessage
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from linebot import LineBotApi
from linebot.models import TextSendMessage

# —— 狀態存儲 —— #
LAST_SEEN_FILE = "last_seen.json"

def load_last_seen():
    if os.path.exists(LAST_SEEN_FILE):
        with open(LAST_SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_last_seen(data):
    with open(LAST_SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# —— LINE Messaging API —— #
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_TOKEN")
if not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("❌ 未設定 LINE_CHANNEL_TOKEN 環境變數")
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)

def send_line_message(text: str):
    try:
        # Broadcast 給所有已加好友的使用者
        line_bot_api.broadcast(TextSendMessage(text=text))
        print("✅ 已 broadcast LINE 訊息")
    except Exception:
        print("❌ Broadcast LINE 訊息失敗：")
        traceback.print_exc()

# —— Gmail 備援通知 —— #
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
        import smtplib
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(user, pwd)
            server.send_message(msg)
        print("✅ Gmail 發信成功")
    except Exception:
        print("❌ Gmail 發信失敗：")
        traceback.print_exc()

# —— Selenium Driver —— #
def create_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.binary_location = "/usr/bin/chromium-browser"
    driver = webdriver.Chrome(options=options)
    driver.implicitly_wait(10)
    return driver

# —— Hermes 列表爬取 —— #
def scrape_hermes():
    url = "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"
    driver = create_driver()
    driver.get(url)
    WebDriverWait(driver, 20).until(lambda d: d.execute_script("return document.readyState") == "complete")
    time.sleep(2)

    links = driver.find_elements(
        By.XPATH,
        "//a[contains(@href, '/product/') and normalize-space(text())!='']"
    )[:5]

    results = []
    for a in links:
        name = a.text.strip()
        href = a.get_attribute("href")
        price = "無標價"
        try:
            li = a.find_element(By.XPATH, "./ancestor::li")
            price = li.find_element(By.CSS_SELECTOR, ".price, .product-price").text.strip()
        except:
            pass
        results.append(f"👜 Hermès：{name}\n💰 {price}\n🔗 {href}")

    driver.quit()
    return results

# —— 2nd STREET 列表爬取 —— #
def scrape_2nd_street(name, url):
    driver = create_driver()
    driver.get(url)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.product-card"))
        )
    except TimeoutException:
        print(f"⚠️ 等待 {name} product-card 超時")
        driver.quit()
        return []
    time.sleep(1)

    cards = driver.find_elements(By.CSS_SELECTOR, "div.product-card")[:5]
    results = []
    for card in cards:
        title = card.find_element(By.CSS_SELECTOR, ".title, .product-name").text.strip()
        href  = card.find_element(By.TAG_NAME, "a").get_attribute("href")
        try:
            price = card.find_element(By.CSS_SELECTOR, ".price, .product-price").text.strip()
        except:
            price = "無標價"
        results.append(f"🏪 {name}：{title}\n💰 {price}\n🔗 {href}")

    driver.quit()
    return results

# —— 主流程 —— #
def job():
    print("⏰ 開始執行爬蟲與通知…")
    last = load_last_seen()
    notify = []
    new_seen = {}

    # Hermes
    hermes = scrape_hermes()
    if hermes:
        first = hermes[0].split("\n🔗 ")[1]
        if last.get("hermes") != first:
            notify += hermes
            new_seen["hermes"] = first

    # 2nd STREET
    for tag, url in [
        ("2nd STREET HERMES", "https://store.2ndstreet.com.tw/v2/Search?q=HERMES&shopId=41320&order=Newest"),
        ("2nd STREET CHANEL", "https://store.2ndstreet.com.tw/v2/Search?q=CHANEL&shopId=41320&order=Newest"),
        ("2nd STREET Dior",   "https://store.2ndstreet.com.tw/v2/Search?q=Christian+Dior&shopId=41320&order=Newest"),
    ]:
        res = scrape_2nd_street(tag, url)
        if res:
            first_url = res[0].split("\n🔗 ")[1]
            key = tag.lower().replace(" ", "_")
            if last.get(key) != first_url:
                notify += res
                new_seen[key] = first_url

    print("DEBUG notify list:", notify)
    if notify:
        header = f"📦 偵測到 {len(notify)} 件新品，前 5 筆：\n\n"
        body = "\n\n".join(notify[:5])
        send_line_message(header + body)
        send_email_message("新品上架通知", header + body)
        last.update(new_seen)
        save_last_seen(last)
    else:
        print("👍 無新商品")

if __name__ == "__main__":
    job()
