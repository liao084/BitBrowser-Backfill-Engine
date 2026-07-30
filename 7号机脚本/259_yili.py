#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数仓任务执行脚本 - 多平台版（统一配置+拼多多自动登录优化） - 20260205
任务名称：抖音电商罗盘+抖音罗盘经营+京东品牌主页+生意参谋+拼多多任务采集
拼多多采用自动登录流程，无需Cookie，其他平台使用Cookie登录
"""

import requests
import json
import os
import pickle
import time
from datetime import datetime, timezone
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException


class DouyinJDTaskRunnerEnhanced:
    """多平台任务执行器（统一配置版+拼多多优化）"""
    
    def __init__(self, bite_id=None):
        """初始化配置 - 统一配置区域"""
        
        # ==================== 平台采集配置 ====================
        # 在这里配置要采集的平台（只需要修改这一个地方）
        
        # 选项1: 只采集抖音电商罗盘
        # self.platforms = ['抖音罗盘经营','拼多多']
        
        # 选项2: 只采集抖音罗盘经营
        # self.platforms = ['抖音罗盘经营']
        
        # 选项3: 只采集京东
        self.platforms = ['京东']
        
        # 选项4: 只采集生意参谋
        # self.platforms = ['生意参谋']
        
        # 选项5: 只采集拼多多（优化版）
        # self.platforms = ['拼多多']
        
        # 选项6: 采集抖音相关平台
        # self.platforms = ['抖音电商罗盘', '抖音罗盘经营']

        # self.platforms = ['抖音电商罗盘']
        
        # 选项7: 采集全部平台（默认）
        # self.platforms = ['抖音电商罗盘', '抖音罗盘经营', '京东', '生意参谋', '拼多多']
        
        # ====================================================
        
        # 比特浏览器配置
        self.bt_url = 'http://127.0.0.1:54345'
        self.headers = {'Content-Type': 'application/json'}

        # self.bite_id = bite_id or '19aac60621344f268d26a6c01a940170' #测试专用
        self.bite_id = bite_id or '4626a1f1fadb4ac4aab182d93469147f' #131
        
        # 任务配置
        self.task_indices = [4978, 4980, 4981, 4990]  # 数仓任务索引
        self.cookie_dir = r'C:\Users\1\Desktop\COOKIE'
        
        # 关键URL - 抖音电商罗盘、抖音罗盘经营、京东、生意参谋和拼多多（优化版）
        self.douyin_brand_url = 'https://compassbrand.jinritemai.com/'  # 抖音电商罗盘（策略）
        self.douyin_shop_url = 'https://compass.jinritemai.com/shop'    # 抖音罗盘经营
        self.jd_home_url = 'https://ppzh.jd.com/brand/homePage/index.html'
        self.sycm_home_url = 'https://sycm.taobao.com/portal/home.htm'
        self.pdd_home_url = 'https://mms.pinduoduo.com/home/'
        self.pdd_passport_url = 'https://passport.pinduoduo.com/'  # 拼多多认证域名
        self.task_url = 'https://datatoolcenter.com/web/dateCenter.html?activeName=selfitemkeyShop&menuplat=%E5%B7%A5%E4%BD%9C%E5%8F%B0'

    def open_browser(self):
        """打开比特浏览器"""
        max_retries = 5
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                url = f'{self.bt_url}/browser/open'
                response = requests.post(url, headers=self.headers, 
                                       data=json.dumps({"id": self.bite_id}))
                
                if response.status_code == 200:
                    result = response.json()
                    
                    # 检查是否成功
                    if result.get('success') == True and 'data' in result:
                        data = result['data']
                        if isinstance(data, dict) and 'driver' in data and 'http' in data:
                            print("✓ 浏览器启动成功")
                            return (data['driver'], data['http'])
                    
                    # 处理各种错误情况
                    elif result.get('success') == False:
                        msg = result.get('msg', '未知错误')
                        
                        if '正在关闭中' in msg or '请稍后操作' in msg:
                            print(f"⏳ 浏览器正在关闭中，等待 {retry_delay} 秒后重试... (尝试 {attempt + 1}/{max_retries})")
                            if attempt < max_retries - 1:  # 不是最后一次尝试
                                time.sleep(retry_delay)
                                continue
                            else:
                                print("✗ 浏览器关闭超时，请手动检查比特浏览器状态")
                                return None
                        else:
                            print(f"✗ 浏览器启动失败: {msg}")
                            return None
                    
                    else:
                        print(f"✗ 响应格式异常: {result}")
                        return None
                        
                else:
                    print(f"✗ 浏览器启动失败: HTTP {response.status_code}")
                    print(f"响应内容: {response.text}")
                    return None
                    
            except Exception as e:
                print(f"✗ 浏览器启动异常: {e}")
                if attempt < max_retries - 1:
                    print(f"⏳ 等待 {retry_delay} 秒后重试...")
                    time.sleep(retry_delay)
                    continue
                return None
        
        print("✗ 多次尝试后仍无法启动浏览器")
        return None

    def close_browser(self):
        """关闭比特浏览器"""
        try:
            url = f'{self.bt_url}/browser/close'
            response = requests.post(url, headers=self.headers, 
                                   data=json.dumps({"id": self.bite_id}))
            
            if response.status_code == 200:
                result = response.json()
                if result.get('success') == True:
                    print("✓ 浏览器关闭成功")
                else:
                    msg = result.get('msg', '未知错误')
                    print(f"⚠️ 浏览器关闭响应: {msg}")
            else:
                print(f"⚠️ 浏览器关闭请求失败: HTTP {response.status_code}")
                
            # 等待一下确保关闭完成
            time.sleep(1)
            
        except Exception as e:
            print(f"⚠️ 关闭浏览器异常: {e}")
            # 关闭失败不影响后续流程，继续执行

    def load_cookies(self, platform='抖音电商罗盘'):
        """加载本地Cookie文件 - 支持合并格式（多平台版+拼多多优化）"""
        # 根据平台确定Cookie键名和文件前缀
        if platform == '抖音电商罗盘':
            cookie_key = '抖音电商罗盘'
            file_prefix = 'douyin_cookies'
        elif platform == '抖音罗盘经营':
            cookie_key = '抖音罗盘经营'
            file_prefix = 'douyin_shop_cookies'
        elif platform == '京东':
            cookie_key = '京东品牌主页'
            file_prefix = 'jd_cookies'
        elif platform == '生意参谋':
            cookie_key = '淘宝生意参谋'
            file_prefix = 'sycm_cookies'
        elif platform == '拼多多':
            # 拼多多不再使用Cookie登录，直接返回None
            print(f"ℹ️ {platform}平台使用自动登录流程，不需要加载Cookie")
            return None
        else:
            print(f"✗ 不支持的平台: {platform}")
            return None
        
        # 优先使用比特ID专用文件（新格式）
        cookie_files = [
            os.path.join(self.cookie_dir, f"{self.bite_id}.pkl"),  # 新的合并格式
            os.path.join(self.cookie_dir, "cookies_latest.pkl"),   # 新的兼容格式
            os.path.join(self.cookie_dir, f"{file_prefix}_{self.bite_id}.pkl"),  # 平台专用格式
            os.path.join(self.cookie_dir, f"{file_prefix}_latest.pkl")  # 平台兼容格式
        ]
        
        for cookie_file in cookie_files:
            if os.path.exists(cookie_file):
                try:
                    with open(cookie_file, 'rb') as f:
                        cookies_data = pickle.load(f)
                    
                    # 检查是否为新的合并格式（字典）
                    if isinstance(cookies_data, dict):
                        # 新格式：从合并的cookie字典中提取指定平台的cookies
                        platform_cookies = cookies_data.get(cookie_key, [])
                        if platform_cookies:
                            print(f"✓ 加载{platform}合并Cookie: {os.path.basename(cookie_file)}")
                            print(f"  包含网站: {list(cookies_data.keys())}")
                            print(f"  {platform}Cookie: {len(platform_cookies)}个")
                            return platform_cookies
                        else:
                            print(f"✗ 合并Cookie文件中未找到{platform}数据: {os.path.basename(cookie_file)}")
                            continue
                    else:
                        # 旧格式：直接使用cookie列表
                        cookies = cookies_data
                        print(f"✓ 加载{platform}Cookie: {os.path.basename(cookie_file)} ({len(cookies)}个)")
                        return cookies
                        
                except Exception as e:
                    print(f"✗ Cookie文件损坏: {os.path.basename(cookie_file)} - {e}")
                    continue
        
        print(f"✗ 未找到有效的{platform}Cookie文件，请先运行save_cookies脚本获取{platform}Cookie")
        return None

    def analyze_login_cookies(self, driver, platform='抖音电商罗盘'):
        """分析登录Cookie有效期 - 多平台版（仅作为补充信息）"""
        try:
            # 获取浏览器当前Cookie
            current_cookies = driver.get_cookies()
            current_time = datetime.now(timezone.utc)
            
            # 根据平台确定登录相关关键词
            if platform == '抖音电商罗盘' or platform == '抖音罗盘经营':
                login_keywords = ['login', 'session', 'auth', 'token', 'douyin', 'jinritemai', 'compass', 'user', 'uid', 'sid']
            elif platform == '京东':
                login_keywords = ['login', 'session', 'auth', 'token', 'jd', 'ppzh', 'user', 'uid', 'sid']
            elif platform == '生意参谋':
                login_keywords = ['login', 'session', 'auth', 'token', 'sycm', 'taobao', 'user', 'uid', 'sid']
            elif platform == '拼多多':
                # 拼多多特殊关键词 - 更全面的识别
                login_keywords = ['login', 'session', 'auth', 'token', 'pdd', 'pinduoduo', 'mms', 'user', 'uid', 'sid', 
                                'merchant', 'passport', 'access_token', 'refresh_token', 'mall_id', 'shop_id', 'JSESSIONID']
            else:
                login_keywords = ['login', 'session', 'auth', 'token', 'user', 'uid', 'sid']
            
            # 筛选有效的登录Cookie
            valid_login_cookies = []
            for cookie in current_cookies:
                name = cookie.get('name', '')
                
                # 检查是否为登录相关Cookie且有过期时间
                if any(keyword in name.lower() for keyword in login_keywords) and 'expiry' in cookie:
                    expiry_time = datetime.fromtimestamp(cookie['expiry'], timezone.utc)
                    
                    if expiry_time > current_time:
                        time_left = expiry_time - current_time
                        valid_login_cookies.append({
                            'name': name,
                            'expiry_time': expiry_time,
                            'hours_left': int(time_left.total_seconds() // 3600)
                        })
            
            if valid_login_cookies:
                # 按过期时间排序，找到最早过期的
                valid_login_cookies.sort(key=lambda x: x['expiry_time'])
                earliest = valid_login_cookies[0]
                
                print(f"\n🔑 {platform}登录状态:")
                print(f"   有效Cookie数: {len(valid_login_cookies)}")
                print(f"   最早过期: {earliest['expiry_time'].strftime('%m-%d %H:%M')}")
                print(f"   剩余时间: {earliest['hours_left']}小时")
                
                # 过期警告（但不影响登录判断）
                if earliest['hours_left'] < 2:
                    print(f"   ⚠️  警告: {platform}登录即将失效，建议更新Cookie")
                
                return True
            else:
                print(f"⚠️ 未找到有效的{platform}登录Cookie（这不影响登录状态判断）")
                return True  # 改为返回True，不影响主要登录判断
                
        except Exception as e:
            print(f"⚠️ 分析{platform}Cookie异常: {e}")
            return True  # 异常时也返回True，不影响主要登录判断

    def setup_driver(self, browser_data):
        """配置WebDriver"""
        selenium_webdriver, selenium_address = browser_data
        
        # Chrome选项配置
        options = ChromeOptions()
        options.add_experimental_option("debuggerAddress", selenium_address)
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-sandbox')
        
        # 创建WebDriver
        service = ChromeService(executable_path=selenium_webdriver)
        driver = webdriver.Chrome(service=service, options=options)
        
        # 反检测配置
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => false})")
        
        return driver

    def ensure_platform_login(self, driver, platform='抖音电商罗盘'):
        """确保指定平台登录状态 - 拼多多优化版"""
        print(f"\n{'='*50}")
        print(f"{platform}登录状态检测")
        print(f"{'='*50}")
        

        # 根据平台选择URL和登录检测逻辑
        if platform == '抖音电商罗盘':
            home_url = self.douyin_brand_url
            login_check = lambda url: "/login" in url
        elif platform == '抖音罗盘经营':
            home_url = self.douyin_shop_url
            login_check = lambda url: "/login" in url
        elif platform == '京东':
            home_url = self.jd_home_url
            login_check = lambda url: "login" in url.lower()
        elif platform == '生意参谋':
            home_url = self.sycm_home_url
            login_check = lambda url: "custom/login.htm" in url
        elif platform == '拼多多':
            # 拼多多使用自动登录流程，不再依赖Cookie
            return self.ensure_pdd_login_enhanced(driver)
        else:
            print(f"✗ 不支持的平台: {platform}")
            return False
        
        # 其他平台的常规处理
        return self.ensure_regular_platform_login(driver, platform, home_url, login_check)

    def ensure_pdd_login_enhanced(self, driver):
        """拼多多登录状态检测 - 自动登录版"""
        print("🔧 使用拼多多优化登录检测流程...")
        
        try:
            # 访问商家后台主页
            print(f"访问商家后台: {self.pdd_home_url}")
            driver.get(self.pdd_home_url)
            time.sleep(5)  # 拼多多需要更长加载时间
            
            # 检查登录状态
            if self.check_pdd_login_status(driver):
                print("✓ 拼多多登录状态正常")
                self.analyze_login_cookies(driver, '拼多多')
                return True
            
            print("✗ 拼多多当前未登录，尝试自动登录流程...")
            
            # 执行自动登录流程
            return self.auto_login_pdd(driver)
            
        except Exception as e:
            print(f"✗ 拼多多登录检测异常: {str(e)}")
            return False

    def check_pdd_login_status(self, driver):
        """检查拼多多登录状态"""
        try:
            current_url = driver.current_url
            page_source = driver.page_source
            
            # 检查登录失效指示器
            login_fail_indicators = [
                "/login" in current_url
            ]
            
            if any(login_fail_indicators):
                print("✗ 检测到登录状态失效或需要登录")
                return False
            
            # 检查登录成功指示器
            login_success_indicators = [
                "login" not in current_url
            ]
            
            if any(login_success_indicators):
                print("✓ 检测到登录状态正常")
                return True
            
            # 尝试查找用户信息元素
            try:
                user_selectors = [
                    ".user-info", ".user-name", ".shop-name", 
                    "[class*='user']", "[class*='shop']", "[class*='merchant']"
                ]
                
                for selector in user_selectors:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        print(f"✓ 找到用户信息元素: {selector}")
                        return True
            except:
                pass
            
            print("⚠️ 无法确定登录状态，可能需要手动检查")
            return False
            
        except Exception as e:
            print(f"⚠️ 登录状态检测异常: {str(e)}")
            return False

    def auto_login_pdd(self, driver):
        """拼多多自动登录流程"""
        print("🔧 开始拼多多自动登录流程...")
        
        try:
            # 等待页面加载完成
            time.sleep(3)
            
            # 第一步：点击切换为账号登录
            try:
                switch_login_xpath = "/html/body/div[2]/div[1]/div/div/main/div/section[2]/div/div/div/div[1]/div/div/div[2]"
                switch_element = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, switch_login_xpath))
                )
                switch_element.click()
                print("✓ 成功点击切换为账号登录")
                time.sleep(2)
            except Exception as e:
                print(f"⚠️ 点击切换登录方式失败，可能已经是账号登录模式: {str(e)}")
            
            # 第二步：点击登录按钮
            try:
                login_button_xpath = "/html/body/div[2]/div[1]/div/div/main/div/section[2]/div/div/div/div[2]/section/div/div/button"
                login_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, login_button_xpath))
                )
                login_button.click()
                print("✓ 成功点击登录按钮")
                time.sleep(3)
            except Exception as e:
                print(f"✗ 点击登录按钮失败: {str(e)}")
                return False
            
            # 第三步：等待用户手动完成登录
            print("第3步：等待用户手动完成登录...")
            print("📱 请在浏览器中手动完成登录操作（扫码或输入账号密码）")
            print("⏳ 脚本将等待最多60秒检测登录状态...")
            
            # 循环检测登录状态，最多等待60秒
            max_wait_time = 60
            check_interval = 3
            elapsed_time = 0
            
            while elapsed_time < max_wait_time:
                time.sleep(check_interval)
                elapsed_time += check_interval
                
                # 检查当前登录状态
                if self.check_pdd_login_status(driver):
                    print(f"✓ 拼多多登录成功！耗时: {elapsed_time}秒")
                    self.analyze_login_cookies(driver, '拼多多')
                    return True
                
                print(f"⏳ 等待登录中... ({elapsed_time}/{max_wait_time}秒)")
            
            # 超时处理
            print("⏰ 等待登录超时，请检查登录状态")
            
            # 最后再检查一次
            if self.check_pdd_login_status(driver):
                print("✓ 拼多多登录成功！")
                return True
            else:
                print("✗ 拼多多登录失败或超时，请手动登录后重新运行脚本")
                return False
                
        except Exception as e:
            print(f"✗ 拼多多自动登录流程异常: {str(e)}")
            return False

    def ensure_regular_platform_login(self, driver, platform, home_url, login_check):
        """常规平台登录状态检测"""
        # 访问平台主页检查登录状态
        driver.get(home_url)
        time.sleep(3)
        
        current_url = driver.current_url
        
        # 检查是否跳转到登录页面
        if login_check(current_url):
            print(f"✗ 当前未登录{platform}，检测到登录页面: {current_url}")
            print(f"✗ 尝试{platform}Cookie登录...")
            
            # 加载并应用Cookie
            cookies = self.load_cookies(platform)
            if not cookies:
                return False
            
            # 应用Cookie
            driver.delete_all_cookies()
            success_count = 0
            
            for cookie in cookies:
                try:
                    cookie_dict = {
                        'name': cookie['name'],
                        'value': cookie['value'],
                        'path': cookie.get('path', '/'),
                    }
                    
                    if 'domain' in cookie:
                        domain = cookie['domain'].lstrip('.')
                        cookie_dict['domain'] = domain
                    
                    driver.add_cookie(cookie_dict)
                    success_count += 1
                except:
                    continue
            
            print(f"✓ 应用{platform}Cookie: {success_count}/{len(cookies)}")
            
            # 重新访问验证
            driver.get(home_url)
            time.sleep(3)
            
            current_url = driver.current_url
            if login_check(current_url):
                print(f"✗ {platform}Cookie登录失败，仍在登录页面: {current_url}")
                print(f"✗ 请手动登录{platform}或更新Cookie")
                return False
        
        print(f"✓ {platform}登录状态正常，当前页面: {current_url}")
        
        # Cookie分析作为补充信息，不影响主要登录判断
        self.analyze_login_cookies(driver, platform)
        
        # URL检测通过就返回True
        return True

    def ensure_login(self, driver):
        """确保配置的平台登录状态"""
        print("\n" + "="*60)
        print("平台登录状态检测（拼多多自动登录版）")
        print("="*60)
        
        login_results = {}
        
        # 只检测配置的平台
        for platform in self.platforms:
            login_results[platform] = self.ensure_platform_login(driver, platform)
        
        # 汇总登录状态
        print(f"\n📊 登录状态汇总:")
        for platform, status in login_results.items():
            status_text = '✓ 已登录' if status else '✗ 未登录'
            if platform == '抖音电商罗盘':
                print(f"   抖音电商罗盘: {status_text}")
            elif platform == '抖音罗盘经营':
                print(f"   抖音罗盘经营: {status_text}")
            elif platform == '京东':
                print(f"   京东品牌主页: {status_text}")
            elif platform == '生意参谋':
                print(f"   淘宝生意参谋: {status_text}")
            elif platform == '拼多多':
                print(f"   拼多多商家后台: {status_text} {'🔧自动登录' if status else '❌需手动登录'}")
        
        # 检查是否至少有一个平台登录成功
        success_count = sum(1 for status in login_results.values() if status)
        
        if success_count > 0:
            print(f"✓ {success_count}/{len(login_results)} 个平台登录成功，可以继续执行任务")
            return True
        else:
            print(f"✗ 所有配置的平台都未登录，无法执行任务")
            return False

    def execute_task(self, driver, task_index):
        """执行单个数据采集任务"""
        try:
            print(f"执行任务 {task_index}...")
            
            input_element = driver.find_element(By.XPATH, '//*[@id="pane-first"]/div[1]/div[2]/input')
            input_element.send_keys(task_index)
            search_btn = driver.find_element(By.XPATH, "//button[.//i[contains(@class, 'el-icon-search')]]")
            search_btn.click()
            time.sleep(3)
            
            # 点击任务
            task_xpath = f"//*[@id='pane-first']/div[2]/div[2]/div"
            WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, task_xpath))
            ).click()
            time.sleep(0.5)
            
            # 设置日期为昨天
            date_xpath = "//*[@id='xiaotool']/div[1]/div[2]/div[1]/div[1]/div/div[3]/div/div/section/div[2]/div[1]/div[1]/div[2]/input[1]"
            WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, date_xpath))
            ).click()
            
            time.sleep(1)
            WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, "/html/body/div[3]/div[1]/div[1]/button[1]"))
            ).click()
            
            # 执行检测
            check_xpath = "//*[@id='checkbutn']/span"
            WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, check_xpath))
            ).click()
            
            # 等待检测完成
            WebDriverWait(driver, 45).until(
                EC.presence_of_element_located((By.XPATH, "//*[@id='xiaotool']/div[1]/div[2]/div[1]/div[1]/div/div[3]/div/div/section/div[2]/div[3]/div/div/div[1]"))
            )
            
            # 获取任务名称
            try:
                name_element = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//*[@id='xiaotool']/div[1]/div[2]/div[1]/div[1]/div/div[3]/div/div/section/div[1]/span/span[1]"))
                )
                task_name = name_element.text
            except:
                task_name = f"任务{task_index}"
            
            # 执行补齐操作
            complete_xpath = "//*[@id='xiaotool']/div[1]/div[2]/div[1]/div[1]/div/div[3]/div/div/section/div[2]/div[1]/div[2]"
            WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, complete_xpath))
            ).click()
            
            time.sleep(3)
            
            # 检查是否需要确认
            try:
                WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable((By.XPATH, "//*[@id='loseDays_shop_btn']"))
                ).click()
                print(f"✓ {task_name} - 执行完成")
            except (NoSuchElementException, TimeoutException):
                print(f"✓ {task_name} - 已补齐")
            
            return True
            
        except Exception as e:
            print(f"✗ 任务{task_index}执行失败: {e}")
            return False

    def keep_alive(self, driver):
        """保持会话活跃 - 简化版"""
        all_windows = driver.window_handles
        print(f"\n保持会话活跃 - 共{len(all_windows)}个窗口")
        
        # 简化的窗口切换，只执行1轮
        for i, window in enumerate(all_windows):
            try:
                driver.switch_to.window(window)
                print(f"切换到窗口 {i+1}/{len(all_windows)}")
                time.sleep(3)
            except:
                # 窗口可能已关闭，重新获取窗口列表
                all_windows = driver.window_handles
                break
        
        print("✓ 会话保持完成")

    def run(self):
        """主执行流程"""
        print("="*60)
        print("数仓任务采集（拼多多自动登录版）")
        print(f"比特浏览器ID: {self.bite_id}")
        print(f"采集平台: {', '.join(self.platforms)}")
        print(f"任务数量: {len(self.task_indices)}")
        if '拼多多' in self.platforms:
            print("🔧 拼多多将使用自动登录流程")
        print("="*60)
        
        self.close_browser()

        # 1. 启动浏览器
        browser_data = self.open_browser()
        if not browser_data:
            return False
        
        driver = None
        try:
            # 2. 配置WebDriver
            driver = self.setup_driver(browser_data)
            
            # 3. 检查配置平台的登录状态
            if not self.ensure_login(driver):
                print("\n✗ 平台登录验证失败，任务终止")
                return False
            
            # 4. 准备任务窗口
            print(f"\n开始执行 {len(self.task_indices)} 个任务...")
            
            # 根据配置选择默认打开的页面
            if '抖音电商罗盘' in self.platforms:
                driver.get(self.douyin_brand_url)
            elif '抖音罗盘经营' in self.platforms:
                driver.get(self.douyin_shop_url)
            elif '京东' in self.platforms:
                driver.get(self.jd_home_url)
            elif '生意参谋' in self.platforms:
                driver.get(self.sycm_home_url)
            elif '拼多多' in self.platforms:
                driver.get(self.pdd_home_url)
            
            # 关闭多余窗口
            main_window = driver.current_window_handle
            for window in driver.window_handles:
                if window != main_window:
                    driver.switch_to.window(window)
                    driver.close()
            driver.switch_to.window(main_window)
            
            # 打开任务窗口
            for i in range(len(self.task_indices)):
                driver.execute_script(f"window.open('{self.task_url}')")
            
            time.sleep(2)
            all_windows = driver.window_handles
            
            # 5. 执行任务
            success_count = 0
            for i, task_index in enumerate(self.task_indices, 1):
                try:
                    driver.switch_to.window(all_windows[i])
                    if self.execute_task(driver, task_index):
                        success_count += 1
                except Exception as e:
                    print(f"✗ 窗口{i}任务异常: {e}")
                    continue
            
            # 6. 保持会话活跃
            self.keep_alive(driver)
            self.keep_alive(driver)
            self.keep_alive(driver)
            
            print(f"\n✓ 任务执行完成: {success_count}/{len(self.task_indices)}")
            return True
            
        except Exception as e:
            print(f"✗ 任务执行过程异常: {e}")
            return False
def main():
    """主函数"""
    import sys
    
    # 支持命令行指定比特ID
    bite_id = sys.argv[1] if len(sys.argv) > 1 else None
    
    # 创建并运行任务执行器
    runner = DouyinJDTaskRunnerEnhanced(bite_id)
    success = runner.run()
    
    if success:
        print(f"\n🎉 所有任务执行完成！")
    else:
        print(f"\n❌ 任务执行失败，请检查日志")
    
    return success


if __name__ == '__main__':
    main()
