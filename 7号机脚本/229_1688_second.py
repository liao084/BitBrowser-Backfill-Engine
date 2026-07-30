import requests
import json
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException


class Bite():
    def __init__(self):
        self.bt_url = 'http://127.0.0.1:54345'
        self.headers = {'Content-Type': 'application/json'}
    # 获取窗口ID
    def create_browser(self):
        url = f'{self.bt_url}/browser/list'

        # body请求参数
        data = {
            "page": 0,
            "pageSize": 20
        }
        res = requests.post(url, data=json.dumps(data), headers=self.headers)
        #如果请求成功，获取返回内容，匹配浏览器窗口id值
        if res.status_code == 200:
            result = json.loads(res.text)
            browser_id = result['data']['list'][2]['id']
            print(browser_id)
            return browser_id
        else:
            print(res.status_code)
            return None
    # 直接指定ID打开窗口，也可以使用 createBrowser 方法返回的ID
    def open_Browser(self, id):
        url = f'{self.bt_url}/browser/open'
        json_data = {"id": f'{id}'}

        res = requests.post(url, headers=self.headers, data=json.dumps(json_data))

        if res.status_code == 200:
            result = json.loads(res.text)

            driver_data = result['data']['driver']
            http_data = result['data']['http']
            return (driver_data, http_data)
        else:
            print(res.status_code)
            return None
    # 检查1688登录状态
    def check_1688_login(self, driver):
        driver.refresh()
        time.sleep(10)
        try:
            login = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//*[@id='login-form']/div[6]/button"))
            )
            if login:
                print("1688掉登,正在登录...")
                #点击登录
                login.click()
                time.sleep(4)

                #判断是否出现"市场"元素，确认登录成功
                market_element = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//*[@id='app']/div/div[1]/div/div/div/div[1]/ul/li[10]/a/span"))
                )
                if market_element:
                    print("1688已登录！")
        except (NoSuchElementException, TimeoutException):
            print("1688已登录！")
    # 处理数仓任务
    def process_data_task(self, driver, task_index):
        try:
            # 按任务 ID 搜索
            input_element = driver.find_element(
                By.XPATH,
                '//*[@id="pane-first"]/div[1]/div[2]/input'
            )
            input_element.send_keys(task_index)
            search_btn = driver.find_element(
                By.XPATH,
                "//button[.//i[contains(@class, 'el-icon-search')]]"
            )
            search_btn.click()
            time.sleep(3)

            # 点击搜索结果中的任务卡片
            task_element = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((
                    By.XPATH,
                    "//*[@id='pane-first']/div[2]/div[2]/div"
                ))
            )
            task_element.click()
            time.sleep(0.2)
            # 点击日期
            date_element = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.XPATH, "//*[@id='xiaotool']/div[1]/div[2]/div[1]/div[1]/div/div[3]/div/div/section/div[2]/div[1]/div[1]/div[2]/input[1]"))
            )
            date_element.click()
            # 点击"前天"
            time.sleep(1)
            day_before_yesterday = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.XPATH, "/html/body/div[3]/div[1]/div[1]/button[2]"))
            )
            day_before_yesterday.click()
            # 点击检测
            check_button = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.XPATH, "//*[@id='checkbutn']/span"))
            )
            check_button.click()
            # 等待检测完成
            WebDriverWait(driver, 60).until(
                EC.presence_of_element_located((By.XPATH, "//*[@id='xiaotool']/div[1]/div[2]/div[1]/div[1]/div/div[3]/div/div/section/div[2]/div[3]/div/div/div[1]"))
            )
            # 获取任务名
            name_element = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, "//*[@id='xiaotool']/div[1]/div[2]/div[1]/div[1]/div/div[3]/div/div/section/div[1]/span/span[1]"))
            )
            name = name_element.text
            # 点击补齐
            complete_button = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.XPATH, "//*[@id='xiaotool']/div[1]/div[2]/div[1]/div[1]/div/div[3]/div/div/section/div[2]/div[1]/div[2]"))
            )
            complete_button.click()

            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//*[@id='loseDays_shop_btn']"))
                ).click()
            except (NoSuchElementException, TimeoutException):
                print(f'{name}___已补齐')

        except (NoSuchElementException, TimeoutException) as e:
            print(f"处理任务 {task_index} 时出错: {str(e)}")
            # 可以添加重试逻辑或记录错误
    # 防止页面休眠的窗口切换
    def switch_windows_to_prevent_sleep(self, driver):
        # 获取当前所有窗口句柄
        all_windows = driver.window_handles
        # 执行3轮页面切换，防止页面休眠
        for round_num in range(1, 4):
            print(f"\n开始第 {round_num} 轮页面切换")
            # 遍历所有窗口
            for i, window in enumerate(all_windows):
                try:
                    # 切换到当前窗口
                    driver.switch_to.window(window)
                    print(f"切换到页面 {i+1}/{len(all_windows)}: {driver.title[:30]}...")
                    # 等待3秒让页面加载
                    time.sleep(3)
                except Exception as e:
                    print(f"页面 {i+1}变动，重新获取窗口句柄")
                    # 如果窗口已关闭，重新获取所有窗口句柄
                    all_windows = driver.window_handles
        print("\n完成3轮页面切换，防止加载失败")
        print("\n任务挂载完成")
    # 通过selenium来操作比特浏览器
    def start_selenium(self, data):
        # 设置访问的目标网站地址
        url = 'https://sycm.1688.com/ms/rival/downStream'

        # 比特浏览器的驱动值，driver值
        selenium_webdriver = data[0]
        # 比特浏览器的域名信息，http值
        selenium_address = data[1]

        # 获取对应的chromedriver驱动
        chromedriver_path = selenium_webdriver

        options = ChromeOptions()
        options.add_experimental_option("debuggerAddress", selenium_address)  # 这行命令必须加上，才能启动指纹浏览器
        service = ChromeService(executable_path=chromedriver_path)
        driver = webdriver.Chrome(service=service, options=options)

        # 设置隐式等待时间
        driver.implicitly_wait(20)

        driver.get(url)

        '''打开三个相同的页面(可设置)'''
        for _ in range(3):
            driver.execute_script("window.open('https://datatoolcenter.com/web/dateCenter.html?activeName=free_analysis_overview&menuplat=%E5%B7%A5%E4%BD%9C%E5%8F%B0&currentMenuIndex=206&dateType=day&runAsUserId=%E5%85%A8%E9%83%A8%E5%BA%97%E9%93%BA')")

        # 获取所有窗口句柄
        windows = driver.window_handles

        # 检查1688登录状态
        driver.switch_to.window(windows[0])
        self.check_1688_login(driver)

        '''处理三个数仓任务(可设置)'''
        task_indices = [2802, 2803, 2804]
        for i, task_index in enumerate(task_indices, 1):
            try:
                driver.switch_to.window(windows[i])
                self.process_data_task(driver, task_index)
            except Exception as e:
                print(f"处理窗口 {i} 的任务时出错: {str(e)}")
                continue  # 继续处理下一个任务

        # 防止页面休眠
        self.switch_windows_to_prevent_sleep(driver)

if __name__ == '__main__':
    print("开始执行_1688_每日任务采集")
    b = Bite()
    #获取比特ID
    browser_id = b.create_browser()
    target_data = b.open_Browser('6048a9aa1e34419c988f1bb1deef7888')
    if target_data:
        #执行自动化操作
        b.start_selenium(target_data)
