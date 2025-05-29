import os
import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
import pandas as pd
import yagmail

# ====== 參數 ======
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "你的_Channel_Access_Token")
LINE_USER_ID = os.getenv("LINE_USER_ID", "你的_userId")
GMAIL_USER = os.getenv("GMAIL_USER", "你的Gmail帳號")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "你的Gmail應用程式密碼")
GMAIL_TO = os.getenv("GMAIL_TO", "收件人信箱")

hermes_urls = [
    ("包包&手拿包", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/bags-and-clutches/"),
    ("小皮件", "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"),
]

second_urls = [
    ("HERMES", "https://store.2ndstreet.com.tw/v2/Search?q=HERMES&shopId=41320&order=Newest"),
    ("CHANEL", "https://store.2ndstreet.com.tw/v2/Search?q=CHANEL&shopId=41320&order=Newest"),
    ("Christian Dior", "https://store.2ndstreet.com.tw/v2/Search?q=Christian+Dior&shopId=41320&order=Newest"),
]

# ===== LINE Messaging API (BOT) 通知 =====
def send_line_bot_message(user_id, text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "to": user_id,
        "messages": [{
            "type": "text",
            "text": text
        }]
    }
    r = requests.post(url, headers=headers, json=data)
    print(f"LINE Messaging API 回應: {r.status_code} {r.text}")
    return r.status_code

# ===== Gmail 通知 =====
def send_gmail(subject, body):
    yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
    yag.send(GMAIL_TO, subject, body)

# ===== Hermès 多分類 =====
hermes_data = []
options = webdriver.ChromeOptions()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
for cname, url in hermes_urls:
    print(f"抓取 Hermès 分類: {cname}")
    driver.get(url)
    time.sleep(5)
    items = driver.find_elements(By.CSS_SELECTOR, "div.product-grid-list-item")
    for item in items:
        try:
            name = item.find_element(By.CSS_SELECTOR, ".product-item-name").text.strip()
            link = item.find_element(By.CSS_SELECTOR, ".product-item-name").get_attribute('href')
            color = item.find_element(By.CSS_SELECTOR, ".product-item-colors").text.strip().replace("顏色:", "").strip()
        except Exception:
            name = link = color = ""
        try:
            price = item.find_element(By.CSS_SELECTOR, ".price").text.strip()
        except Exception:
            price = ""
        try:
            img = item.find_element(By.CSS_SELECTOR, "img").get_attribute("src")
        except Exception:
            img = ""
        hermes_data.append({
            "source": f"Hermès官網 {cname}",
            "name": name,
            "color": color,
            "price": price,
            "link": link,
            "img": img
        })
driver.quit()

# ===== 2nd STREET 多品牌 =====
second_data = []
for brand, url in second_urls:
    print(f"抓取 2nd STREET: {brand}")
    res = requests.get(url)
    soup = BeautifulSoup(res.text, 'html.parser')
    items = soup.select('div.p-list__item')
    for item in items:
        try:
            name = item.select_one('h2.p-list__item__name').text.strip()
            link = "https://store.2ndstreet.com.tw" + item.select_one('a.p-list__item__inner')['href']
            price = item.select_one('div.p-list__item__price').text.strip().replace('\n', '')
            img = item.select_one('img')['src']
        except Exception:
            name = link = price = img = ""
        second_data.append({
            "source": f"2nd STREET {brand}",
            "name": name,
            "color": "",
            "price": price,
            "link": link,
            "img": img
        })

# ===== 合併資料 =====
data = hermes_data + second_data
df = pd.DataFrame(data)
df.to_csv('hermes_and_2ndstreet.csv', index=False, encoding='utf-8-sig')

# ===== 判斷新品/價格異動 only =====
notify_list = []
try:
    if os.path.exists('last_seen.csv'):
        last = pd.read_csv('last_seen.csv')
        last_set = set(last['name'] + "|" + last['price'])
    else:
        last_set = set()
except Exception:
    last_set = set()

for _, row in df.iterrows():
    key = f"{row['name']}|{row['price']}"
    if key not in last_set:
        msg = f"[{row['source']}]\n{row['name']} {row.get('color', '')} {row['price']}\n{row['link']}"
        notify_list.append(msg)

notify_msg = "\n\n".join(notify_list)

# ===== LINE BOT 通知（只通知新品/變價） =====
if notify_msg and "你的_Channel_Access_Token" not in CHANNEL_ACCESS_TOKEN and "你的_userId" not in LINE_USER_ID:
    send_line_bot_message(LINE_USER_ID, notify_msg)

# ===== Gmail =====
if notify_msg and "你的Gmail帳號" not in GMAIL_USER:
    send_gmail("Hermès/2nd STREET 新上架商品", notify_msg)

# ===== 更新 last_seen.csv 檔案 =====
df.to_csv('last_seen.csv', index=False, encoding='utf-8-sig')

print("只推播新品/變價商品，完成！")
