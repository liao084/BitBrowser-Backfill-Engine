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



class BackfillEngine:
    """历史数据补采引擎"""

    def __init__(self, bite_id: str):
        self.bt_url = 'http://127.0.0.1:54345'
        self.bite_id = bite_id
        # 心跳静默判定机制超时时间（秒）
        self.silent_timeout_seconds = 120
        # 互斥锁：用于解决插件开新标签页时的并发捕获冲突（串台问题）
        self.tab_lock = asyncio.Lock()

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

    async def _delayed_check(self, page: Page):
        """延迟检测新网页 URL 并部署后台监控任务"""
        try:
            # 等待最多 10 秒，让网页跳转到真实的 URL
            for _ in range(10):
                if page.is_closed():
                    return
                if "ppzh.jd.com" in page.url:
                    # 确认为商智网页，部署监控协程
                    url_suffix = page.url[-25:] if len(page.url) > 25 else page.url
                    logger.info(f"[GC Daemon] 发现商智网页，开始后台监控: {url_suffix}")
                    asyncio.create_task(self._monitor_and_gc_page(page))
                    return
                await asyncio.sleep(1)
        except Exception:
            pass

    def _on_new_page(self, page: Page):
        """拦截浏览器新建标签页的事件"""
        asyncio.create_task(self._delayed_check(page))

    async def _monitor_and_gc_page(self, page: Page):
        """
        后台垃圾回收协程：基于事件倒计时监控特定商智页面的心跳。
        """
        toast_selector = ".el-message__content:has-text('同步成功')"
        url_suffix = page.url[-25:] if len(page.url) > 25 else page.url
        
        while not page.is_closed():
            try:
                # 等待心跳弹窗，超时时间为 180 秒
                await page.wait_for_selector(toast_selector, state="attached", timeout=180000)
                # 等待弹窗消失，防止重复触发
                await page.wait_for_selector(toast_selector, state="detached", timeout=15000)
                logger.debug(f"[GC Daemon] 商智网页 {url_suffix} 心跳正常。")
            except PlaywrightTimeoutError:
                # 180 秒内无任何心跳，判定为残留僵尸网页
                if not page.is_closed():
                    logger.warning(f"[GC Daemon] 商智网页 {url_suffix} 超过 180 秒无心跳，判定为残留任务，执行强制关闭。")
                    try:
                        await page.close()
                    except Exception as e:
                        logger.warning(f"[GC Daemon] 关闭网页发生异常: {e}")
                break
            except Exception:
                # 网页可能在这期间被正常关闭了
                break
        
        logger.debug(f"[GC Daemon] 网页 {url_suffix} 监控任务结束。")

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

    async def inject_dates(self, page: Page, start_date: str, end_date: str, worker_id: str):
        """
        核心日期修改逻辑：改用 Playwright 真实键盘模拟，破解 Vue 的双向绑定失效问题。
        """
        logger.info(f"Worker-{worker_id} 开始注入采集区间: {start_date} 至 {end_date}")
        try:
            inputs = page.locator("input.el-range-input")
            
            # 填充开始日期并按回车确认
            await inputs.nth(0).fill(start_date)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(200) # 给 UI 一点反应时间
            
            # 填充结束日期并按回车确认
            await inputs.nth(1).fill(end_date)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(200)
            
            # 为什么不用 JS evaluate？因为 JS 强行赋值往往无法触发 ElementUI 内部的 v-model 更新，导致点击检测时依旧使用了默认日期。
        except Exception as e:
            logger.error(f"Worker-{worker_id} 日期注入失败: {e}")
            raise

    async def wait_for_completion_or_heartbeat(self, page: Page, worker_id: str):
        """
        基于心跳静默的完工判定机制与弹窗清理。
        如果在指定的静默期内没有再出现新的心跳弹窗，则认为能够补采的数据已经全部下发完毕（或已卡死）。
        结束后关闭多余残留弹窗。
        """
        toast_selector = ".el-message__content:has-text('同步成功')"
        silent_timeout_ms = self.silent_timeout_seconds * 1000 
        
        logger.info(f"Worker-{worker_id} 开始静默监听（超过 {self.silent_timeout_seconds} 秒无心跳则判定完工/卡死）...")
        
        while True:
            try:
                # 等待绿色弹窗出现
                await page.wait_for_selector(toast_selector, state="attached", timeout=silent_timeout_ms)
                logger.debug(f"Worker-{worker_id} 收到心跳弹窗，正在采集中...")
                
                # 等待该弹窗消失，准备迎接下一次弹窗
                await page.wait_for_selector(toast_selector, state="detached", timeout=15000)
                
            except PlaywrightTimeoutError:
                logger.info(f"Worker-{worker_id} 超过 {self.silent_timeout_seconds} 秒无新心跳，判定当前区间补采结束或卡死。")
                break
            except Exception as e:
                logger.error(f"Worker-{worker_id} 监听过程中发生未知异常: {e}")
                raise e
                
        # 通过三级弹窗的可见性判定是否卡死
        logger.info(f"Worker-{worker_id} 心跳停止，开始探测卡死状态...")
        stuck_indicator = page.locator("div.dialog-title:visible")
        
        if await stuck_indicator.count() > 0:
            logger.warning(f"Worker-{worker_id} 检测到三级弹窗依然存在，判定为卡死，准备关闭多层弹窗...")
            close_btns = page.locator("i.el-icon-close:visible")
            
            # 依次点击关闭按钮（分别关闭三级和二级弹窗）
            for _ in range(2):
                if await close_btns.count() > 0:
                    try:
                        await close_btns.last.click(force=True)
                        await page.wait_for_timeout(1000) # 等待动画消失
                    except Exception as e:
                        logger.warning(f"Worker-{worker_id} 关闭卡死弹窗失败: {e}")
            logger.info(f"Worker-{worker_id} 卡死弹窗清理完毕，退回主页面。")
        else:
            logger.info(f"Worker-{worker_id} 未检测到三级弹窗，正常采集完成。")

    async def worker(self, task_card_index: int, page: Page, date_chunks: List[Tuple[str, str]]):
        """Worker 协程：处理指定页面上的所有日期分块任务"""
        worker_id = f"卡片-{task_card_index}"
        logger.info(f"Worker-{worker_id} 启动，绑定页面: {page.url[-25:]}")
        
        # --- 页面初始化清理 ---
        try:
            logger.info(f"Worker-{worker_id} 执行页面初始化清理...")
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
            
            # 不需要记录基准线，依靠锚点即可
        except Exception as e:
            logger.error(f"Worker-{worker_id} 初始化失败，跳过该标签页: {e}")
            return # 初始化失败，退出当前任务
            
        # --- 循环处理日期分块任务 ---
        for start_date, end_date in date_chunks:
            logger.info(f"Worker-{worker_id} 开始处理任务: {start_date} 至 {end_date}")
            
            try:
                # 每轮开始前检查：如果上一次意外遗留了三级弹窗，直接关掉
                stuck_indicator = page.locator("div.dialog-title:visible")
                if await stuck_indicator.count() > 0:
                    logger.warning(f"Worker-{worker_id} 循环开始前发现残留的卡死弹窗，进行清理...")
                    
                    close_btns = page.locator("i.el-icon-close:visible")
                    for _ in range(2):
                        if await close_btns.count() > 0:
                            try:
                                await close_btns.last.click(force=True)
                                await page.wait_for_timeout(500)
                            except Exception:
                                pass

                # 1. 注入时间
                await self.inject_dates(page, start_date, end_date, worker_id)
                


                # 2. 启动检测
                start_btn_selector = "#checkbutn"
                # 使用 force=True 确保点击触发，避免被其他元素遮挡
                await page.click(start_btn_selector, force=True)
                
                # --- 等待查询结果容器渲染 ---
                # 给系统一点时间销毁旧容器（如果存在）
                await page.wait_for_timeout(500)
                
                logger.info(f"Worker-{worker_id} 等待查询结果容器渲染 (最多45秒)...")
                try:
                    await page.wait_for_selector('.testContent_list:has-text("京东商智")', state='visible', timeout=45000)
                    logger.info(f"Worker-{worker_id} 查询结果已渲染完成！")
                except PlaywrightTimeoutError:
                    logger.warning(f"Worker-{worker_id} 等待查询结果超时(45s)，仍继续尝试下一步。")
                
                # 保险起见，给 500ms 让后续的按钮 DOM 渲染完全
                await page.wait_for_timeout(500)
                
                # 点击补齐数据
                logger.info(f"Worker-{worker_id} 点击补齐数据按钮...")
                backfill_btn_selector = "span.lostDataBtn:visible"
                
                # 无论是否有缺失数据，按钮都存在，直接点击
                if await page.locator(backfill_btn_selector).count() > 0:
                    await page.locator(backfill_btn_selector).first.click(force=True)
                else:
                    # 防错处理：如果网页还没加载出按钮，等待后重试
                    logger.warning(f"Worker-{worker_id} 找不到补齐数据按钮，重试一次...")
                    await page.wait_for_selector(backfill_btn_selector, timeout=5000)
                    await page.locator(backfill_btn_selector).first.click(force=True)
                
                logger.info(f"Worker-{worker_id} 正在探测是否有【无需补齐】提示...")
                try:
                    # 如果 3 秒内等到了“无需补齐”的 Toast，说明真没数据缺失
                    await page.wait_for_selector(".el-message__content:has-text('无需补齐')", state="attached", timeout=3000)
                    logger.info(f"Worker-{worker_id} 检测到【无需补齐】提示，当日无缺失数据，跳过该日期！")
                    
                    # 关键修复：等待该绿色提示框彻底消失再 continue，防止把残影带入下一天的检测
                    logger.info(f"Worker-{worker_id} 等待【无需补齐】提示框消失...")
                    await page.wait_for_selector(".el-message__content:has-text('无需补齐')", state="detached", timeout=5000)
                    continue # 采集完成（无缺失），直接进入下一个循环
                except PlaywrightTimeoutError:
                    # 3秒还没等到 Toast，说明没有出现“无需补齐”，那么必然是弹出了二级弹窗
                    logger.info(f"Worker-{worker_id} 未检测到【无需补齐】，说明进入了二级弹窗，继续后续补齐流程...")
                

                whole_store_btn_selector = "#loseDays_shop_btn"
                
                # 捕获点击“全店补齐”后新开的蓝页标签
                logger.info(f"Worker-{worker_id} 确认存在缺失数据，准备获取全局锁以安全捕获新标签页...")
                new_tab = None
                try:
                    # 获取互斥锁：确保同一瞬间只有一个 worker 在触发插件弹窗，彻底解决串台问题
                    async with self.tab_lock:
                        async with page.context.expect_page(timeout=8000) as new_page_info:
                            await page.click(whole_store_btn_selector, force=True)
                        new_tab = await new_page_info.value
                    logger.info(f"Worker-{worker_id} 成功安全捕获到新弹出的京东数智标签页: {new_tab.url}")
                except PlaywrightTimeoutError:
                    logger.warning(f"Worker-{worker_id} 点击全店补齐后，5秒内未捕获到新标签页，将继续在主页面监听。")
                    # 如果超时没捕获到，可能网络慢或按钮触发失败，确保已经点击了
                except Exception as e:
                    logger.warning(f"Worker-{worker_id} 捕获新标签页时出现异常: {e}")
                
                # 4. 开始完工判定（交由 GC 守护进程管理蓝页，此处只需判定本页弹窗状态）
                await self.wait_for_completion_or_heartbeat(page, worker_id)
                logger.info(f"Worker-{worker_id} 成功跑完任务: {start_date} 至 {end_date}")
                
            except Exception as e:
                error_msg = str(e).lower()
                # 死亡检测：如果浏览器被外部程序（如主管的定时脚本）物理关闭，直接跳出大循环，停止无意义的报错刷屏
                if "closed" in error_msg or "disconnected" in error_msg or "target page" in error_msg:
                    logger.error(f"Worker-{worker_id} 检测到浏览器连接已断开，停止任务执行并退出。")
                    break
                    
                logger.error(f"Worker-{worker_id} 在执行 {start_date} 至 {end_date} 期间发生错误: {e}")
                logger.warning(f"Worker-{worker_id} 由于执行异常，将跳过 {start_date} 至 {end_date} 区间，继续尝试下一个。")
                await asyncio.sleep(5)

        logger.info(f"Worker-{worker_id} 所有任务处理完毕。")

        # --- 阶段三：收尾（保留现场） ---
        logger.info(f"Worker-{worker_id} 完工！遵照指示保留当前任务弹窗，供人工复核。")
    async def run(self, tasks_config: list = None):
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
            
            # --- 挂载全局 GC 守护进程 ---
            context.on("page", self._on_new_page)
            # 把现存的网页也拉进去扫描一遍
            for existing_page in context.pages:
                self._on_new_page(existing_page)
                
            pages = context.pages
            
            # 过滤出符合数据中心 URL 的标签页作为 Workers
            worker_pages = [page for page in pages if "datatoolcenter" in page.url]
            
            if not worker_pages:
                logger.error("未找到对应的数据检测工具网页，请确认浏览器中是否已打开目标页面！")
                return
                
            logger.info(f"检测到 {len(worker_pages)} 个符合条件的 Worker 标签页。")

            if not tasks_config:
                logger.error("未传入任何任务配置 tasks_config，引擎停止运行。")
                return

            # 为了兼容老代码（如果用户传入的是 dict）
            if isinstance(tasks_config, dict):
                tasks_list = [{"card": k, **v} for k, v in tasks_config.items()]
            else:
                tasks_list = tasks_config

            # 并发处理：按配置列表从 worker_pages 池中取网页派发任务
            worker_tasks = []
            assigned_count = min(len(tasks_list), len(worker_pages))
            
            if len(tasks_list) > len(worker_pages):
                logger.warning(f"⚠️ 你配置了 {len(tasks_list)} 个任务，但浏览器中只找到了 {len(worker_pages)} 个标签页，资源不足，多余的任务将被忽略！")
                
            for i in range(assigned_count):
                config = tasks_list[i]
                task_card_index = config.get("card", config.get("task_card_index", 1)) # 获取目标卡片
                page = worker_pages[i] # 从浏览器池中获取对应的页面实例
                
                logger.info(f"已将大盘任务卡片 {task_card_index} 分配给后台标签页 {i+1}")
                
                # 动态生成该任务专属的 date_chunks
                date_chunks = self.generate_date_chunks(config["start"], config["end"], config["chunk_days"])
                
                # 将协程任务加入列表，把 task_card_index 传进去
                worker_tasks.append(self.worker(task_card_index=task_card_index, page=page, date_chunks=date_chunks))
            
            if worker_tasks:
                logger.info(f"\n{'='*40}\n开始并发执行 {len(worker_tasks)} 个标签页任务...\n{'='*40}")
                # 并发执行所有组装好的协程任务
                await asyncio.gather(*worker_tasks)

            logger.info("\n所有并发定向补采任务执行完毕。")

if __name__ == "__main__":
    bite_id = '4626a1f1fadb4ac4aab182d93469147f'
    # 任务配置列表：每个字典代表分配给一个标签页的采集任务
    # 注意：支持多个标签页同时采集同一个卡片（比如 4 个标签页同时跑卡片 5，但日期不同）
    tasks_config = [
        {"card": 3, "start": "2026-05-01", "end": "2026-05-31", "chunk_days": 1},
        {"card": 5, "start": "2025-07-01", "end": "2025-07-15", "chunk_days": 1},
        {"card": 5, "start": "2025-07-16", "end": "2025-07-31", "chunk_days": 1},
        {"card": 5, "start": "2025-08-01", "end": "2025-08-15", "chunk_days": 1},
        {"card": 5, "start": "2025-08-16", "end": "2025-08-31", "chunk_days": 1},
    ]
    
    engine = BackfillEngine(bite_id)
    asyncio.run(engine.run(tasks_config))
