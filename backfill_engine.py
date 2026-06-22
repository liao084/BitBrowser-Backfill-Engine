#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RPA 历史数据自动化补采调度引擎 (Playwright Async 版)
基于比特浏览器架构，支持多标签页并发任务分配与心跳防卡死监控。
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

import requests
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

# 配置日志
# 配置日志（同时输出到控制台和本地文件）
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("BackfillEngine")
logger.setLevel(logging.INFO)

# 1. 输出到控制台
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# 2. 输出到本地文件 (会自动在 exe 同目录下生成 backfill_run.log)
try:
    file_handler = logging.FileHandler('backfill_run.log', encoding='utf-8')
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)
except Exception as e:
    print(f"无法创建日志文件: {e}")

# 全局安全防走火开关 (True: 填完日期就永远挂起，不点检测不点补采)
SAFE_MODE = False


class BackfillEngine:
    """历史数据补采引擎"""

    def __init__(self, bite_id: str):
        self.bt_url = 'http://127.0.0.1:54345'
        self.bite_id = bite_id
        # 心跳静默判定机制超时时间（秒）
        self.silent_timeout_seconds = 120

    def get_debugger_address(self) -> Optional[str]:
        """
        【复用说明】
        复用了原 `131_pdd_lpjy.py` 中的 `open_browser` 核心逻辑。
        修改点：
        1. 剥离了直接启动 Selenium WebDriver 的逻辑。
        2. 仅保留通过 HTTP 接口唤醒比特浏览器，并获取 CDP 调试地址（如 127.0.0.1:xxxx）的逻辑。
        """
        logger.info(f"正在尝试连接比特浏览器 (ID: {self.bite_id})...")
        url = f'{self.bt_url}/browser/open'
        headers = {'Content-Type': 'application/json'}
        try:
            response = requests.post(url, headers=headers, json={"id": self.bite_id}, timeout=15)
            if response.status_code == 200:
                result = response.json()
                if result.get('success') and 'data' in result:
                    cdp_address = result['data'].get('http')
                    logger.info(f"✓ 获取浏览器 CDP 调试地址成功: {cdp_address}")
                    return cdp_address
                else:
                    logger.error(f"✗ 浏览器启动响应错误: {result.get('msg')}")
            else:
                logger.error(f"✗ API 请求失败 HTTP {response.status_code}")
        except Exception as e:
            logger.error(f"✗ 浏览器连接异常: {str(e)}")
        return None

    def generate_date_chunks(self, start_date_str: str, end_date_str: str, chunk_days: int = 3) -> List[Tuple[str, str]]:
        """生成任务队列分块，返回日期段列表"""
        fmt = "%Y-%m-%d"
        current_date = datetime.strptime(start_date_str, fmt)
        end_date = datetime.strptime(end_date_str, fmt)

        chunks = []
        while current_date <= end_date:
            chunk_end = current_date + timedelta(days=chunk_days - 1)
            if chunk_end > end_date:
                chunk_end = end_date
            
            chunks.append((current_date.strftime(fmt), chunk_end.strftime(fmt)))
            current_date = chunk_end + timedelta(days=1)
            
        logger.info(f"✓ 任务队列生成完毕，共切分为 {len(chunks)} 个任务块")
        return chunks

    async def inject_dates(self, page: Page, start_date: str, end_date: str):
        """
        核心日期修改逻辑：优先尝试 JS 注入。
        注：这里使用的是伪选择器，需要后续在实际 DOM 中替换真实的 input selector
        """
        logger.info(f"[{page.url[-20:]}] 开始注入采集区间: {start_date} 至 {end_date}")
        try:
            # 结合 ElementUI 的真实 DOM 结构：两个 input 共享同一个 class
            # 通过 querySelectorAll 获取数组，第0个是开始日期，第1个是结束日期
            await page.evaluate(f'''() => {{
                const inputs = document.querySelectorAll("input.el-range-input");
                if (inputs.length >= 2) {{
                    const startInput = inputs[0];
                    const endInput = inputs[1];
                    
                    startInput.value = "{start_date}";
                    startInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    startInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    
                    endInput.value = "{end_date}";
                    endInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    endInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }}''')
            # 按钮点击逻辑已移至 worker 主循环，以配合 SAFE_MODE 控制
        except Exception as e:
            logger.error(f"[{page.url[-20:]}] 日期注入失败: {e}")
            raise

    async def wait_for_completion_or_heartbeat(self, page: Page):
        """
        基于心跳静默的完工判定机制。
        如果在指定的静默期内没有再出现新的心跳弹窗，则认为能够补采的数据已经全部下发完毕。
        """
        # 使用更精准的内部容器加文本断言，防止误判其他颜色的弹窗
        toast_selector = ".el-message__content:has-text('同步成功')"
        silent_timeout_ms = self.silent_timeout_seconds * 1000 
        
        logger.info(f"[{page.url[-20:]}] 开始静默监听（超过 {self.silent_timeout_seconds} 秒无心跳则判定完工）...")
        
        while True:
            try:
                # 等待绿色弹窗出现
                await page.wait_for_selector(toast_selector, state="attached", timeout=silent_timeout_ms)
                logger.debug(f"[{page.url[-20:]}] 💓 收到心跳弹窗，正在采集中...")
                
                # 等待该弹窗消失，准备迎接下一次弹窗
                await page.wait_for_selector(toast_selector, state="detached", timeout=15000)
                
            except PlaywrightTimeoutError:
                # 核心逻辑：如果在 silent_timeout_ms 内没有新弹窗出现，抛出超时，我们在此捕获并视作“完工”！
                logger.info(f"[{page.url[-20:]}] 🎉 超过 {self.silent_timeout_seconds} 秒无新心跳，判定当前区间补采结束！")
                return True
            except Exception as e:
                logger.error(f"[{page.url[-20:]}] 监听过程中发生未知异常: {e}")
                raise e

    async def worker(self, task_card_index: int, page: Page, date_chunks: List[Tuple[str, str]]):
        """Worker 协程：处理指定页面上的所有日期分块任务"""
        worker_id = f"卡片-{task_card_index}"
        logger.info(f"Worker-{worker_id} 启动，绑定页面: {page.url[-25:]}")
        
        # --- 阶段一：页面初始化清理 ---
        try:
            logger.info(f"Worker-{worker_id} 正在执行接管初始化清理...")
            # 1. 尝试关闭当前弹窗（如果存在可见的 X 号）
            # 注意：使用 :visible 伪类，确保只找肉眼看得见的叉号，避免点到隐藏的残留 DOM
            close_dialog_btn = "i.el-icon-close:visible" 
            if await page.locator(close_dialog_btn).count() > 0:
                await page.locator(close_dialog_btn).first.click()
                await page.wait_for_timeout(1000) # 等待弹窗动画消失
                
            # 2. 点击进入“补采专属任务卡片”
            nth_index = task_card_index - 1
            backfill_card_selector = f"div.workTool_page_card_test_dataCard >> nth={nth_index}"
            await page.click(backfill_card_selector)
            
            # 等待补采详情弹窗加载出来
            await page.wait_for_selector("span:has-text('缺失数据')", state="visible", timeout=15000)
            logger.info(f"Worker-{worker_id} 初始化完成，已成功进入补采专属弹窗！")
        except Exception as e:
            logger.error(f"Worker-{worker_id} 初始化失败，跳过该标签页: {e}")
            return # 初始化失败直接罢工
            
        # --- 阶段二：补采任务大循环 (针对每个日期分块串行执行) ---
        for start_date, end_date in date_chunks:
            logger.info(f"Worker-{worker_id} 开始处理任务: {start_date} 至 {end_date}")
            
            try:
                # 1. 注入时间
                await self.inject_dates(page, start_date, end_date)
                
                # --- 安全物理保险 ---
                if SAFE_MODE:
                    logger.info(f"Worker-{worker_id} 🛡️ [SAFE_MODE] 已填入日期 {start_date} 至 {end_date}。触发安全锁定，无限期挂起供人工检阅！")
                    await asyncio.sleep(86400) # 休眠一天
                    continue

                # 2. 启动检测
                start_btn_selector = "#checkbutn"
                await page.click(start_btn_selector)
                
                # --- 智能等待逻辑：动态捕捉 Loading 遮罩 ---
                try:
                    # 对于半年的大数据量查询，前端可能需要较长的时间才会弹出 loading 遮罩
                    # 这里放宽到 15 秒来捕捉遮罩的出现
                    await page.wait_for_selector('.el-loading-mask', state='visible', timeout=15000)
                    # 既然成功捕捉到了它的出现，就严格等待它彻底消失（最长 60 秒，大数据量耗时更久）
                    await page.wait_for_selector('.el-loading-mask', state='hidden', timeout=60000)
                except PlaywrightTimeoutError:
                    # 如果 15 秒内都没看到遮罩层，直接放行
                    logger.info(f"Worker-{worker_id} 未捕捉到 loading 遮罩或遮罩瞬间消失，放行下一步...")
                
                # 保险起见，给 500ms 让后续的补齐按钮 DOM 渲染完全
                await page.wait_for_timeout(500)
                
                # 点击补齐连招
                logger.info(f"Worker-{worker_id} 点击补齐，进入采集环节...")
                backfill_btn_selector = "span.lostDataBtn"
                # 在点击补齐前，确保按钮是存在并且可点击的。
                # 如果找不到，说明没数据缺失。
                btn_count = await page.locator(backfill_btn_selector).count()
                if btn_count == 0:
                    logger.info(f"Worker-{worker_id} 未检测到补齐按钮，可能无缺失数据，完美跳过。")
                    continue
                    
                await page.click(backfill_btn_selector)
                await page.wait_for_timeout(1000) # 等待二级弹窗
                
                whole_store_btn_selector = "#loseDays_shop_btn"
                await page.click(whole_store_btn_selector)
                
                # 4. 开始心跳监控和完工判定
                await self.wait_for_completion_or_heartbeat(page)
                logger.info(f"Worker-{worker_id} 成功跑完任务: {start_date} 至 {end_date}")
                
            except Exception as e:
                logger.error(f"Worker-{worker_id} 在执行 {start_date} 至 {end_date} 期间发生错误: {e}")
                # 既然是顺序执行，遇到错误我们可以选择稍作等待，让下个循环继续尝试，或者直接跳过
                logger.warning(f"由于执行异常，将跳过 {start_date} 至 {end_date} 区间，继续下一个。")
                await asyncio.sleep(5)

        logger.info(f"Worker-{worker_id} 所有任务处理完毕。")

        # --- 阶段三：收尾清理 ---
        try:
            logger.info(f"Worker-{worker_id} 正在执行完工清理，关闭补采弹窗...")
            close_dialog_btn = "i.el-icon-close >> nth=0" 
            if await page.locator(close_dialog_btn).count() > 0:
                await page.click(close_dialog_btn)
                await page.wait_for_timeout(1000)
            logger.info(f"Worker-{worker_id} 界面还原成功。")
        except Exception as e:
            logger.error(f"Worker-{worker_id} 界面还原失败: {e}")
    async def run(self, tasks_config: dict = None):
        cdp_address = self.get_debugger_address()
        if not cdp_address:
            logger.error("无法获取浏览器 CDP 地址，程序退出")
            return

        async with async_playwright() as p:
            # 连接现有比特浏览器
            browser = await p.chromium.connect_over_cdp(f"http://{cdp_address}")
            contexts = browser.contexts
            
            if not contexts:
                logger.error("浏览器中没有可用的 Context")
                return
                
            context = contexts[0]
            pages = context.pages
            
            # 过滤出符合数据中心 URL 的标签页作为 Workers
            worker_pages = [page for page in pages if "datatoolcenter" in page.url]
            
            if not worker_pages:
                logger.error("未找到对应的数据检测工具网页，请确认浏览器中是否已打开目标页面！")
                return
                
            logger.info(f"检测到 {len(worker_pages)} 个符合条件的 Worker 标签页。")

            if tasks_config is None:
                # 兼容旧模式：如果不传配置，默认点击第 1 个任务卡片
                tasks_config = {1: {"start": "2026-06-01", "end": "2026-06-20", "chunk_days": 3}}

            # 并发处理：按配置字典从 worker_pages 池中取网页派发任务
            worker_tasks = []
            tasks_items = list(tasks_config.items())
            assigned_count = min(len(tasks_items), len(worker_pages))
            
            if len(tasks_items) > len(worker_pages):
                logger.warning(f"⚠️ 你配置了 {len(tasks_items)} 个任务，但浏览器中只找到了 {len(worker_pages)} 个标签页，资源不足，多余的任务将被忽略！")
                
            for i in range(assigned_count):
                task_card_index, config = tasks_items[i]
                page = worker_pages[i] # 顺手牵羊拿池子里的前 i 个网页当打工人
                
                logger.info(f"已将大盘任务卡片 {task_card_index} 分配给后台标签页 {i+1}")
                
                # 动态生成该任务专属的 date_chunks
                date_chunks = self.generate_date_chunks(config["start"], config["end"], config["chunk_days"])
                
                # 将协程任务加入列表，把 task_card_index 传进去
                worker_tasks.append(self.worker(task_card_index=task_card_index, page=page, date_chunks=date_chunks))
            
            if worker_tasks:
                logger.info(f"\n{'='*40}\n开始火力全开！并发执行 {len(worker_tasks)} 个标签页任务...\n{'='*40}")
                # 并发执行所有组装好的协程任务
                await asyncio.gather(*worker_tasks)

            logger.info("\n🎉 所有配置字典中的并发定向补采任务已全部分配并执行完毕！")

if __name__ == "__main__":
    bite_id = '4626a1f1fadb4ac4aab182d93469147f'
    # 任务配置字典：键为【任务大盘里的第几个任务卡片】（人类习惯的第 1, 2, 3 个），值为补采配置
    # 注意：这里的 3 代表大盘里的第 3 个卡片，脚本会自动拉取空闲的标签页去点击它
    tasks_config = {
        3: {"start": "2025-07-01", "end": "2025-07-31", "chunk_days": 3},
        5: {"start": "2025-07-01", "end": "2025-07-31", "chunk_days": 3}
    }
    
    engine = BackfillEngine(bite_id)
    asyncio.run(engine.run(tasks_config))
