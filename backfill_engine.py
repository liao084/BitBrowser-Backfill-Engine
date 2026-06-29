#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RPA 历史数据自动化补采调度引擎 (Playwright Async 版)
基于比特浏览器架构，支持多标签页并发任务分配与心跳防卡死监控。
"""

import asyncio
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple

import requests
from playwright.async_api import async_playwright, Locator, Page, TimeoutError as PlaywrightTimeoutError

# 配置全局日志（同时输出到控制台和本地文件）
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("BackfillEngine")
logger.setLevel(logging.INFO)

# 1. 输出到控制台
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# 2. 输出到本地文件：打包后位于 exe 同目录，源码运行时位于脚本同目录。
runtime_dir = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
log_path = runtime_dir / "backfill_run.log"
try:
    file_handler = logging.FileHandler(log_path, encoding='utf-8')
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)
except Exception as e:
    print(f"无法创建日志文件 {log_path}: {e}")



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

            try:
                # 等待弹窗消失，防止重复触发
                await page.wait_for_selector(toast_selector, state="detached", timeout=15000)
            except PlaywrightTimeoutError:
                if not page.is_closed():
                    logger.warning(f"[GC Daemon] 商智网页 {url_suffix} 心跳弹窗超过 15 秒未消失，判定页面状态异常，执行强制关闭。")
                    try:
                        await page.close()
                    except Exception as e:
                        logger.warning(f"[GC Daemon] 关闭网页发生异常: {e}")
                break
            except Exception:
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

    def _primary_drawer(self, page: Page) -> Locator:
        """一级弹窗：内部包含【启动检测】按钮的 Drawer。"""
        return page.locator("div.el-drawer.rtl").filter(
            has=page.locator("#checkbutn")
        )

    def _secondary_drawer(self, page: Page) -> Locator:
        """二级弹窗：内部包含【全店补齐】按钮的 Drawer。"""
        return page.locator("div.el-drawer.rtl").filter(
            has=page.locator("#loseDays_shop_btn")
        )

    def _progress_dialog(self, page: Page) -> Locator:
        """三级弹窗：包含采集进度标题的可见 Dialog。"""
        return page.locator("div.el-dialog:visible").filter(
            has=page.locator("div.dialog-title")
        )

    async def _close_layer_if_visible(
        self,
        container: Locator,
        layer_name: str,
        worker_id: str,
        timeout_ms: int = 8000,
    ) -> bool:
        """关闭指定容器内唯一的叉号，并等待该容器真正隐藏。"""
        if await container.count() == 0 or not await container.is_visible():
            return False

        close_button = container.locator("i.el-icon-close")
        close_count = await close_button.count()
        if close_count != 1:
            raise RuntimeError(
                f"Worker-{worker_id} {layer_name}内部预期 1 个关闭按钮，实际找到 {close_count} 个"
            )

        logger.info(f"Worker-{worker_id} 正在关闭{layer_name}...")
        await close_button.click(timeout=5000)
        # ElementUI Drawer 关闭后会保留在 DOM 中并变成零尺寸，hidden 可同时兼容隐藏和移除。
        await container.wait_for(state="hidden", timeout=timeout_ms)
        logger.info(f"Worker-{worker_id} {layer_name}已关闭。")
        return True

    async def _restore_primary_state(self, page: Page, worker_id: str):
        """依次关闭三级、二级弹窗，恢复到可操作的一级弹窗。"""
        await self._close_layer_if_visible(
            self._progress_dialog(page), "三级进度弹窗", worker_id
        )
        await self._close_layer_if_visible(
            self._secondary_drawer(page), "二级补采弹窗", worker_id
        )

        primary_drawer = self._primary_drawer(page)
        await primary_drawer.wait_for(state="visible", timeout=10000)
        # trial 只做完整可点击性检查，不触发实际检测。
        await primary_drawer.locator("#checkbutn").click(trial=True, timeout=5000)

    async def _close_all_task_layers(self, page: Page, worker_id: str):
        """Worker 初始化时按层级关闭三级、二级和一级弹窗。"""
        await self._close_layer_if_visible(
            self._progress_dialog(page), "三级进度弹窗", worker_id
        )
        await self._close_layer_if_visible(
            self._secondary_drawer(page), "二级补采弹窗", worker_id
        )
        await self._close_layer_if_visible(
            self._primary_drawer(page), "一级任务弹窗", worker_id
        )

    async def inject_dates(self, page: Page, start_date: str, end_date: str, worker_id: str):
        """
        日期注入逻辑：通过模拟真实的物理键盘事件，确保触发 Vue 框架的数据双向绑定。
        """
        logger.info(f"Worker-{worker_id} 开始注入采集区间: {start_date} 至 {end_date}")
        try:
            primary_drawer = self._primary_drawer(page)
            inputs = primary_drawer.locator("input.el-range-input")

            input_count = await inputs.count()
            if input_count < 2:
                raise RuntimeError(f"一级弹窗内预期至少 2 个日期输入框，实际找到 {input_count} 个")
            
            # 填充开始日期并按回车确认
            await inputs.nth(0).fill(start_date)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(200) # 给 UI 一点反应时间
            
            # 填充结束日期并按回车确认
            await inputs.nth(1).fill(end_date)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(200)
            
            # 避免使用直接赋值，以确保 ElementUI 内部的 v-model 能够正确捕捉到数据变更。
        except Exception as e:
            logger.error(f"Worker-{worker_id} 日期注入失败: {e}")
            raise

    async def wait_for_completion_or_heartbeat(self, page: Page, worker_id: str) -> bool:
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
            except PlaywrightTimeoutError:
                logger.info(f"Worker-{worker_id} 超过 {self.silent_timeout_seconds} 秒无新心跳，判定当前区间补采结束或卡死。")
                break
            except Exception as e:
                logger.error(f"Worker-{worker_id} 监听过程中发生未知异常: {e}")
                raise e

            try:
                # 等待该弹窗消失，准备迎接下一次弹窗
                await page.wait_for_selector(toast_selector, state="detached", timeout=15000)
            except PlaywrightTimeoutError:
                logger.warning(f"Worker-{worker_id} 心跳弹窗超过 15 秒未消失，停止心跳监听并进入终态检查。")
                break
            except Exception as e:
                logger.error(f"Worker-{worker_id} 等待心跳弹窗消失时发生异常: {e}")
                raise
                
        # 心跳停止后，通过精确绑定的三级弹窗判定是否卡死。
        logger.info(f"Worker-{worker_id} 心跳停止，开始探测卡死状态...")
        progress_dialog = self._progress_dialog(page)

        if await progress_dialog.count() > 0 and await progress_dialog.is_visible():
            logger.warning(f"Worker-{worker_id} 检测到三级弹窗依然存在，判定为卡死，准备按层级清理...")
            await self._restore_primary_state(page, worker_id)
            logger.info(f"Worker-{worker_id} 卡死弹窗清理完毕，退回主页面。")
            return False
        else:
            logger.info(f"Worker-{worker_id} 未检测到三级弹窗，正常采集完成。")
            return True

    async def worker(self, task_card_index: int, page: Page, date_chunks: List[Tuple[str, str]], list_index: int):
        """Worker 协程：处理指定页面上的所有日期分块任务"""
        worker_id = f"卡片-{task_card_index}[{list_index}]"
        logger.info(f"Worker-{worker_id} 启动，绑定页面: {page.url[-25:]}")
        
        # --- 页面初始化清理 ---
        try:
            logger.info(f"Worker-{worker_id} 执行页面初始化清理...")
            # 1. 按层级清理可能遗留的三级、二级和一级弹窗。
            await self._close_all_task_layers(page, worker_id)
                
            # 2. 点击进入“补采专属任务卡片”
            nth_index = task_card_index - 1
            backfill_card_selector = f"div.workTool_page_card_test_dataCard >> nth={nth_index}"
            await page.click(backfill_card_selector)
            
            # 等待包含启动按钮的一级弹窗真正展开。
            primary_drawer = self._primary_drawer(page)
            await primary_drawer.wait_for(state="visible", timeout=15000)
            await primary_drawer.locator("#checkbutn").click(trial=True, timeout=5000)
            logger.info(f"Worker-{worker_id} 初始化完成，已成功进入补采专属弹窗！")
            
            # 不需要记录基准线，依靠锚点即可
        except Exception as e:
            logger.error(f"Worker-{worker_id} 初始化失败，跳过该标签页: {e}")
            return # 初始化失败，退出当前任务
            
        # --- 循环处理日期分块任务 ---
        for start_date, end_date in date_chunks:
            logger.info(f"Worker-{worker_id} 开始处理任务: {start_date} 至 {end_date}")
            
            try:
                # 每轮开始前都恢复到一级弹窗，避免二级容器遮挡启动按钮。
                await self._restore_primary_state(page, worker_id)
                primary_drawer = self._primary_drawer(page)

                # 1. 注入时间
                await self.inject_dates(page, start_date, end_date, worker_id)
                


                # 2. 启动检测
                start_btn = primary_drawer.locator("#checkbutn")
                # 保留 Playwright 的遮挡检查；被二级弹窗覆盖时不再强制点击底层按钮。
                await start_btn.click(timeout=10000)
                
                # --- 等待查询结果容器渲染 ---
                # 给系统一点时间销毁旧容器（如果存在）
                await page.wait_for_timeout(500)
                
                logger.info(f"Worker-{worker_id} 等待查询结果容器渲染 (最多45秒)...")
                result_list = primary_drawer.locator('.testContent_list')
                try:
                    await result_list.wait_for(state='visible', timeout=45000)
                    logger.info(f"Worker-{worker_id} 查询结果已渲染完成！")
                except PlaywrightTimeoutError:
                    logger.warning(f"Worker-{worker_id} 等待查询结果超时(45s)，仍继续尝试下一步。")
                
                # 缓冲 1000ms 让顶部的统计状态栏渲染完全
                await page.wait_for_timeout(1000)
                
                # --- 防抖读取统计文本判定缺失状态 ---
                is_missing_data = True # 默认兜底为存在缺失数据
                try:
                    for retry_idx in range(3):
                        missing_span = primary_drawer.locator('div.testContent > div:nth-child(2) > span:nth-child(1)')
                        await missing_span.wait_for(state='attached', timeout=10000)
                        missing_text = await missing_span.inner_text()
                        
                        # 兼容负数情况，支持提取前置的负号
                        match = re.search(r'-?\d+', missing_text)
                        
                        if not match:
                            # 彻底没有提取到数字，说明真的是无缺失数据（纯文本 "[ 有条 缺失数据 ]"）
                            logger.info(f"Worker-{worker_id} 当前区间: {start_date} 至 {end_date}，未检测到数字标识 [{missing_text}]，确认为无缺失数据，跳过！")
                            is_missing_data = False
                            break
                        
                        missing_count = int(match.group())
                        if missing_count > 0:
                            logger.info(f"Worker-{worker_id} 当前区间: {start_date} 至 {end_date}，识别到统计文本 [{missing_text}]，确认缺失 {missing_count} 条数据。")
                            is_missing_data = True
                            break
                        else:
                            # 提取到了 0 或 负数，前端渲染假象 Bug
                            if retry_idx < 2:
                                logger.warning(f"Worker-{worker_id} 提取到异常缺失量 0 或 负数 (前端渲染假象)，第 {retry_idx+1} 次重新点击【启动检测】...")
                                await start_btn.click(timeout=10000)
                                
                                # 重新点击启动检测后，等待数据容器重新渲染
                                await page.wait_for_timeout(500) # 缓冲系统销毁旧 DOM 的时间
                                try:
                                    await result_list.wait_for(state='visible', timeout=45000)
                                    logger.info(f"Worker-{worker_id} 第 {retry_idx+1} 次重试：检测容器已重新渲染成功！")
                                except PlaywrightTimeoutError:
                                    pass
                                await page.wait_for_timeout(500) # 缓冲统计状态栏渲染完全
                            else:
                                logger.warning(f"Worker-{worker_id} 重试 3 次后统计文本仍显示 0 或 负数，放弃重试，强制触发补齐流程兜底！")
                                is_missing_data = True
                except Exception as e:
                    logger.warning(f"Worker-{worker_id} 读取统计文本发生异常: {e}，将强制走补齐流程防错...")

                if not is_missing_data:
                    continue

                # --- 既然有缺失数据（或探测异常兜底），则走后续补齐流程 ---
                logger.info(f"Worker-{worker_id} 准备点击一级补齐数据按钮...")
                backfill_btn = primary_drawer.locator("span.lostDataBtn")
                await backfill_btn.wait_for(state="visible", timeout=5000)
                await backfill_btn.click(timeout=10000)
                
                # --- 二级弹窗处理与全店补齐 ---
                secondary_drawer = self._secondary_drawer(page)
                whole_store_btn = secondary_drawer.locator("#loseDays_shop_btn")
                
                logger.info(f"Worker-{worker_id} 等待二级弹窗渲染 (最多45秒)...")
                click_success = False
                try:
                    # 等待内部包含全店补齐按钮的二级 Drawer 展开，不再误命中一级 Drawer。
                    await secondary_drawer.wait_for(state="visible", timeout=45000)
                    logger.info(f"Worker-{worker_id} 二级弹窗已安全渲染！")
                    
                    # 弹窗重试与恢复策略：遭遇 UI 遮挡等异常时，主动关闭抽屉并重新拉起
                    for click_retry in range(3):
                        try:
                            # 按钮只从已确认的二级容器内定位。
                            await whole_store_btn.wait_for(state="visible", timeout=5000)
                            # 依赖 Playwright 原生拦截检测机制，若有遮挡则主动抛出异常进入恢复流
                            await whole_store_btn.click(timeout=10000)
                            logger.info(f"Worker-{worker_id} 点击【全店补齐】指令发送成功！")
                            click_success = True
                            break
                        except Exception as e:
                            logger.warning(f"Worker-{worker_id} 第 {click_retry+1} 次点击全店补齐失败(可能遭遇subtree遮挡): {str(e)[:100]}...")
                            if click_retry < 2:
                                logger.info(f"Worker-{worker_id} 启动恢复流程：关闭二级弹窗并重新打开...")
                                # 1. 精确关闭三级/二级容器，并确认一级按钮可操作。
                                await self._restore_primary_state(page, worker_id)
                                
                                # 2. 重新点击一级弹窗的补齐按钮
                                logger.info(f"Worker-{worker_id} 重新点击一级补齐数据按钮...")
                                await backfill_btn.click(timeout=10000)
                                
                                # 3. 等待二级弹窗重新渲染
                                await secondary_drawer.wait_for(state="visible", timeout=15000)
                        
                except PlaywrightTimeoutError:
                    logger.error(f"Worker-{worker_id} 二级弹窗打开、按钮点击或恢复流程发生超时。")
                except Exception as e:
                    logger.error(f"Worker-{worker_id} 二级弹窗处理阶段发生未知异常: {e}")

                if not click_success:
                    logger.error(f"Worker-{worker_id} 全店补齐未成功提交，当前区间不进入心跳判定。")
                    try:
                        await self._restore_primary_state(page, worker_id)
                    except Exception as cleanup_error:
                        logger.warning(f"Worker-{worker_id} 提交失败后清理弹窗异常: {cleanup_error}")
                    logger.warning(f"Worker-{worker_id} 跳过 {start_date} 至 {end_date}，继续下一个区间。")
                    continue
                
                # 4. 开始完工判定（交由 GC 守护进程管理蓝页，此处只需判定本页弹窗状态）
                completed_normally = await self.wait_for_completion_or_heartbeat(page, worker_id)
                if completed_normally:
                    logger.info(f"Worker-{worker_id} 成功跑完任务: {start_date} 至 {end_date}")
                else:
                    logger.warning(f"Worker-{worker_id} 当前区间判定为卡死并已清理: {start_date} 至 {end_date}")
                
            except Exception as e:
                error_msg = str(e).lower()
                # 死亡检测：如果浏览器被外部程序（如主管的定时脚本）物理关闭，直接跳出大循环，停止无意义的报错刷屏
                if "closed" in error_msg or "disconnected" in error_msg or "target page" in error_msg:
                    logger.error(f"Worker-{worker_id} 检测到浏览器连接已断开，停止任务执行并退出。")
                    break
                    
                logger.error(f"Worker-{worker_id} 在执行 {start_date} 至 {end_date} 期间发生错误: {e}")
                logger.warning(f"Worker-{worker_id} 由于执行异常，将跳过 {start_date} 至 {end_date} 区间，继续尝试下一个。")
                await asyncio.sleep(5)

        logger.info(f"Worker-{worker_id} 所有任务区间已遍历完毕。")

        # --- 阶段三：收尾（保留现场） ---
        logger.info(f"Worker-{worker_id} 调度结束，保留当前任务弹窗供人工复核。")
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
                
                # 将协程任务加入列表，把 task_card_index 和当前的 list_index 传进去
                worker_tasks.append(self.worker(task_card_index=task_card_index, page=page, date_chunks=date_chunks, list_index=i))
            
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
        {"card": 3, "start": "2025-07-01", "end": "2025-12-31", "chunk_days": 1},
        {"card": 5, "start": "2025-09-01", "end": "2025-09-30", "chunk_days": 1},
        {"card": 5, "start": "2025-10-01", "end": "2025-10-31", "chunk_days": 1},
        {"card": 5, "start": "2025-11-01", "end": "2025-11-30", "chunk_days": 1},
        {"card": 5, "start": "2025-12-01", "end": "2025-12-31", "chunk_days": 1},
    ]
    
    engine = BackfillEngine(bite_id)
    asyncio.run(engine.run(tasks_config))
