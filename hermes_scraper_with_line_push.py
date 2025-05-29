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

# ===== 參數（填你自己的資料） =====
LINE_TOKEN = os.getenv("LINE_NOTIFY_TOKEN", "你的_LINE_NOTIFY_TOKEN")
GMAIL_USER = os.getenv("GMAIL_USER", "你的Gmail帳號")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "你的Gmail應用程式密碼")
GMAIL_TO = os.getenv("GMAIL_TO", "收件人信箱")

hermes_url = "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"
second_url = "https://store.2ndstreet.com.tw/v2/Search?q=HERMES&shopId=41320&order=Newest"

# ===== 通知 function =====
def send_line_notify(msg):
    url = "https://notify-api.line.me/api/notify"
    headers = {"Authorization": f"Bearer {LINE_TOKEN}"}
    data = {"message": msg}
    r = requests.post(url, headers=headers, data=data)
    return r.status_code

def send_gmail(subject, body):
    yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
    yag.send(GMAIL_TO, subject, body)

# ===== Hermès 官網 =====
hermes_data = []
options = webdriver.ChromeOptions()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
driver.get(hermes_url)
time.sleep(5)  # 有時 GitHub Actions 速度較慢

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
        "source": "Hermès官網",
        "name": name,
        "color": color,
        "price": price,
        "link": link,
        "img": img
    })
driver.quit()

# ===== 2nd STREET =====
second_data = []
res = requests.get(second_url)
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
        "source": "2nd STREET",
        "name": name,
        "color": "",
        "price": price,
        "link": link,
        "img": img
    })

# ===== 合併與通知（此範例只推播每站第一個商品，可改進為偵測新上架） =====
data = hermes_data + second_data
df = pd.DataFrame(data)
df.to_csv('hermes_and_2ndstreet.csv', index=False, encoding='utf-8-sig')

# 整理訊息格式
msg_list = []
if len(hermes_data) > 0:
    d = hermes_data[0]
    msg_list.append(f"[Hermès官網]\n{d['name']} {d['color']} {d['price']}\n{d['link']}")

if len(second_data) > 0:
    d = second_data[0]
    msg_list.append(f"[2nd STREET]\n{d['name']} {d['price']}\n{d['link']}")

notify_msg = "\n\n".join(msg_list)

# ===== LINE Notify =====
if notify_msg and "你的_LINE_NOTIFY_TOKEN" not in LINE_TOKEN:
    send_line_notify(notify_msg)

# ===== Gmail =====
if notify_msg and "你的Gmail帳號" not in GMAIL_USER:
    send_gmail("Hermès/2nd STREET 新上架商品", notify_msg)

print("爬蟲＋通知完成！")
