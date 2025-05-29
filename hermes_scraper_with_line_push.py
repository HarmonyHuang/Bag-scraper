from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
import time
import requests
from bs4 import BeautifulSoup
import pandas as pd
import yagmail

# ====== LINE Notify 設定 ======
LINE_TOKEN = 'YOUR_LINE_NOTIFY_TOKEN'  # <--換成你的Token

def send_line_notify(msg):
    url = "https://notify-api.line.me/api/notify"
    headers = {"Authorization": f"Bearer {LINE_TOKEN}"}
    data = {"message": msg}
    requests.post(url, headers=headers, data=data)

# ====== Gmail 設定 ======
GMAIL_USER = 'your_gmail@gmail.com'  # <-- 換成你的Gmail帳號
GMAIL_APP_PASSWORD = 'your_gmail_app_password'  # <-- Gmail「應用程式密碼」
GMAIL_TO = 'your_gmail@gmail.com'  # 可以寄自己或其他信箱

def send_gmail(subject, body):
    yag = yagmail.SMTP(GMAIL_USER, GMAIL_APP_PASSWORD)
    yag.send(GMAIL_TO, subject, body)

# ====== Hermes 官網爬蟲 ======
hermes_url = "https://www.hermes.com/tw/zh/category/women/bags-and-small-leather-goods/small-leather-goods/"
hermes_data = []

options = webdriver.ChromeOptions()
options.add_argument('--headless')
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
driver.get(hermes_url)
time.sleep(3)

items = driver.find_elements(By.CSS_SELECTOR, "div.product-grid-list-item")
for item in items:
    try:
        name = item.find_element(By.CSS_SELECTOR, ".product-item-name").text.strip()
        link = item.find_element(By.CSS_SELECTOR, ".product-item-name").get_attribute('href')
        color = item.find_element(By.CSS_SELECTOR, ".product-item-colors").text.strip().replace("顏色:", "").strip()
    except:
        name = link = color = ""
    try:
        price = item.find_element(By.CSS_SELECTOR, ".price").text.strip()
    except:
        price = ""
    try:
        img = item.find_element(By.CSS_SELECTOR, "img").get_attribute("src")
    except:
        img = ""
    hermes_data.append({
        "source": "Hermes官網",
        "name": name,
        "color": color,
        "price": price,
        "link": link,
        "img": img
    })
driver.quit()

# ====== 2nd STREET爬蟲 ======
second_url = "https://store.2ndstreet.com.tw/v2/Search?q=HERMES&shopId=41320&order=Newest"
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
    except:
        name = link = price = img = ""
    second_data.append({
        "source": "2nd STREET",
        "name": name,
        "color": "",
        "price": price,
        "link": link,
        "img": img
    })

# ====== 合併資料並找最新上架（舉例取每家第一個商品） ======
data = hermes_data + second_data
df = pd.DataFrame(data)

# 通知內容（示範只發每個來源第一筆，實際可比對新舊資料發現新上架）
msg_list = []
if len(hermes_data) > 0:
    d = hermes_data[0]
    msg_list.append(f"[Hermes官網] {d['name']} {d['color']} {d['price']}\n{d['link']}")

if len(second_data) > 0:
    d = second_data[0]
    msg_list.append(f"[2nd STREET] {d['name']} {d['price']}\n{d['link']}")

# 合併通知
notify_msg = "\n\n".join(msg_list)

# ====== 發送 LINE 通知 ======
if notify_msg:
    send_line_notify(notify_msg)

# ====== 發送 Gmail 通知 ======
if notify_msg:
    send_gmail("Hermès/2nd STREET 新上架商品", notify_msg)

# ====== 儲存 CSV 檔案 ======
df.to_csv('hermes_and_2ndstreet.csv', index=False, encoding='utf-8-sig')

print("爬蟲+通知完成！")
