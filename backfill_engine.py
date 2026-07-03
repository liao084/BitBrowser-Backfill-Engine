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
from typing import Any, Dict, Optional, List, Tuple

import requests
from playwright.async_api import (
    ElementHandle,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from task_ledger import TaskLedger

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
        # 红色错误提示保留时间：既给人工观察留出窗口，也避免长期堆积遮挡页面。
        self.error_toast_grace_seconds = 30
        self._error_toast_close_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _fatal_page_error_reason(error: Exception) -> Optional[str]:
        """识别页面崩溃、关闭或浏览器断连等无法继续执行的致命异常。"""
        error_msg = str(error).lower()

        if "page crashed" in error_msg or "target crashed" in error_msg:
            return "页面或其渲染目标已崩溃"

        connection_markers = (
            "closed",
            "disconnected",
            "target page",
        )
        if any(marker in error_msg for marker in connection_markers):
            return "页面、Context或浏览器已关闭或连接断开"

        return None

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

    def _track_error_toast_close_task(self, task: asyncio.Task) -> None:
        """持有延迟关闭任务，避免任务被垃圾回收，并在结束后自动移除。"""
        self._error_toast_close_tasks.add(task)
        task.add_done_callback(self._error_toast_close_tasks.discard)

    async def _close_error_toast_after_grace_period(
        self,
        page: Page,
        toast_handle: ElementHandle,
        worker_id: str,
        message: str,
    ) -> None:
        """保留错误提示一段时间后，点击该提示节点自己的关闭按钮。"""
        close_button: Optional[ElementHandle] = None
        try:
            logger.warning(
                f"Worker-{worker_id} 检测到红色错误提示 [{message}]，"
                f"将在 {self.error_toast_grace_seconds} 秒后自动关闭。"
            )
            await asyncio.sleep(self.error_toast_grace_seconds)

            if page.is_closed() or not await toast_handle.is_visible():
                return

            close_button = await toast_handle.query_selector(
                "i.el-message__closeBtn.el-icon-close"
            )
            if close_button is None:
                logger.warning(
                    f"Worker-{worker_id} 红色错误提示 [{message}] 未找到专属关闭按钮，"
                    "保留该提示供人工处理。"
                )
                return

            # 优先保留 Playwright 的可点击性检查。提示被业务弹窗、loading mask
            # 等页面层遮挡时，仅对这个已经精确绑定的专属叉号降级为 DOM 点击。
            try:
                await close_button.click(timeout=5000)
            except PlaywrightTimeoutError:
                logger.info(
                    f"Worker-{worker_id} 红色错误提示 [{message}] 的关闭按钮被页面层遮挡，"
                    "改用精准 DOM 点击。"
                )
                await close_button.evaluate("node => node.click()")

            try:
                await toast_handle.wait_for_element_state("hidden", timeout=5000)
            except PlaywrightTimeoutError:
                logger.warning(
                    f"Worker-{worker_id} 已点击红色错误提示 [{message}] 的关闭按钮，"
                    "但提示节点在 5 秒内仍未隐藏。"
                )
                return

            logger.info(f"Worker-{worker_id} 已自动关闭红色错误提示 [{message}]。")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not page.is_closed():
                error_summary = str(error).splitlines()[0]
                logger.warning(
                    f"Worker-{worker_id} 自动关闭红色错误提示 [{message}] 失败: "
                    f"{error_summary}；该节点本轮不再重复调度。"
                )
        finally:
            # 节点一旦进入本流程就保留已调度标记；即使关闭失败，也不再每隔
            # 30 秒重复处理同一个节点。新产生的错误提示仍会被监控器独立捕获。
            if close_button is not None:
                try:
                    await close_button.dispose()
                except Exception:
                    pass
            try:
                await toast_handle.dispose()
            except Exception:
                pass

    async def _monitor_worker_error_toasts(self, page: Page, worker_id: str) -> None:
        """监控单个 Worker 页面的红色错误提示，并为每个节点独立安排回收。"""
        pending_selector = (
            "div.el-message.el-message--error.is-closable"
            ":visible:not([data-rpa-error-close-scheduled])"
        )
        logger.info(
            f"Worker-{worker_id} 红色错误提示事件监控已启动；"
            f"提示将保留 {self.error_toast_grace_seconds} 秒后自动关闭。"
        )

        while not page.is_closed():
            try:
                # 与商智 GC 相同：没有目标元素时长期挂起，不做固定频率的 DOM 扫描。
                # 已安排处理的节点带有标记，因此新提示出现后才会重新满足选择器。
                toast_handle = await page.wait_for_selector(
                    pending_selector,
                    state="visible",
                    timeout=0,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if page.is_closed() or self._fatal_page_error_reason(error):
                    break
                logger.warning(
                    f"Worker-{worker_id} 红色错误提示事件等待发生异常，将继续监控: {error}"
                )
                await asyncio.sleep(2)
                continue

            if toast_handle is None:
                # visible 状态正常不会返回 None，仅作为接口返回值的防御性处理。
                continue

            content_handle: Optional[ElementHandle] = None
            try:
                # 标记当前具体节点；下一轮 wait_for_selector 将只等待其他未处理提示。
                await toast_handle.evaluate(
                    "node => node.setAttribute('data-rpa-error-close-scheduled', 'true')"
                )
                content_handle = await toast_handle.query_selector(
                    ".el-message__content"
                )
                message = (
                    (await content_handle.inner_text()).strip()
                    if content_handle is not None
                    else "未读取到错误内容"
                )
                close_task = asyncio.create_task(
                    self._close_error_toast_after_grace_period(
                        page,
                        toast_handle,
                        worker_id,
                        message,
                    )
                )
                self._track_error_toast_close_task(close_task)
            except Exception as error:
                try:
                    await toast_handle.evaluate(
                        "node => node.removeAttribute('data-rpa-error-close-scheduled')"
                    )
                except Exception:
                    pass
                try:
                    await toast_handle.dispose()
                except Exception:
                    pass
                if not page.is_closed():
                    logger.warning(
                        f"Worker-{worker_id} 注册红色错误提示回收任务失败: {error}"
                    )
                # 避免同一个异常节点在注册失败时形成无间隔重试。
                await asyncio.sleep(1)
            finally:
                if content_handle is not None:
                    try:
                        await content_handle.dispose()
                    except Exception:
                        pass

        logger.debug(f"Worker-{worker_id} 红色错误提示监控结束。")

    async def _stop_error_toast_monitors(
        self,
        monitor_tasks: List[asyncio.Task],
    ) -> None:
        """停止 Worker 错误提示监控和仍在等待宽限期的关闭任务。"""
        for task in monitor_tasks:
            task.cancel()
        if monitor_tasks:
            await asyncio.gather(*monitor_tasks, return_exceptions=True)

        close_tasks = list(self._error_toast_close_tasks)
        for task in close_tasks:
            task.cancel()
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        self._error_toast_close_tasks.clear()

    async def _monitor_and_gc_page(self, page: Page):
        """
        后台垃圾回收协程：基于事件倒计时监控特定商智页面的心跳。
        """
        toast_selector = ".el-message__content:has-text('同步成功')"
        url_suffix = page.url[-25:] if len(page.url) > 25 else page.url
        
        while not page.is_closed():
            try:
                # 保存当前这一个心跳节点，避免连续出现的同类提示让 detached 永远无法成立。
                toast_handle = await page.wait_for_selector(
                    toast_selector,
                    state="attached",
                    timeout=180000,
                )
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

            if toast_handle is None:
                # attached 状态正常不会返回 None，仅作为接口返回值的防御性处理。
                continue

            try:
                # 只等待当前节点隐藏或移除；后续成功提示不会替换本次等待目标。
                await toast_handle.wait_for_element_state("hidden", timeout=15000)
            except PlaywrightTimeoutError:
                if not page.is_closed():
                    logger.warning(f"[GC Daemon] 商智网页 {url_suffix} 当前心跳弹窗节点超过 15 秒仍未隐藏，判定页面状态异常，执行强制关闭。")
                    try:
                        await page.close()
                    except Exception as e:
                        logger.warning(f"[GC Daemon] 关闭网页发生异常: {e}")
                break
            except Exception:
                break
            finally:
                try:
                    await toast_handle.dispose()
                except Exception:
                    # 页面正常关闭或崩溃时，释放句柄失败不影响GC协程退出。
                    pass
        
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
                # 保存当前这一个心跳节点。后续只跟踪它，不让新出现的同类提示替换等待目标。
                toast_handle = await page.wait_for_selector(
                    toast_selector,
                    state="attached",
                    timeout=silent_timeout_ms,
                )
            except PlaywrightTimeoutError:
                logger.info(f"Worker-{worker_id} 超过 {self.silent_timeout_seconds} 秒无新心跳，判定当前区间补采结束或卡死。")
                break
            except Exception as e:
                logger.error(f"Worker-{worker_id} 监听过程中发生未知异常: {e}")
                raise

            if toast_handle is None:
                # attached 状态正常不会返回 None，仅作为接口返回值的防御性处理。
                continue

            try:
                # ElementHandle 固定指向当前节点；其他成功提示即使同时出现，也不会影响本次等待。
                # hidden 同时兼容节点隐藏和从 DOM 中移除。
                await toast_handle.wait_for_element_state("hidden", timeout=15000)
            except PlaywrightTimeoutError:
                logger.warning(f"Worker-{worker_id} 当前心跳弹窗节点超过 15 秒仍未隐藏，停止心跳监听并进入终态检查。")
                break
            except Exception as e:
                logger.error(f"Worker-{worker_id} 等待心跳弹窗消失时发生异常: {e}")
                raise
            finally:
                try:
                    await toast_handle.dispose()
                except Exception:
                    # 页面关闭或崩溃时，释放句柄本身也可能失败，不覆盖原始异常。
                    pass
                
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

    def build_tasks(self, tasks_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把配置中的日期范围拆成共享任务池使用的唯一任务。"""
        tasks: List[Dict[str, Any]] = []
        seen_task_ids = set()

        for config in tasks_list:
            card = int(config.get("card", config.get("task_card_index", 1)))
            date_chunks = self.generate_date_chunks(
                config["start"],
                config["end"],
                config.get("chunk_days", 3),
            )
            for start_date, end_date in date_chunks:
                task_id = f"card-{card}_{start_date}_{end_date}"
                if task_id in seen_task_ids:
                    logger.warning(f"检测到重复任务 {task_id}，已跳过重复配置。")
                    continue

                seen_task_ids.add(task_id)
                tasks.append(
                    {
                        "task_id": task_id,
                        "card": card,
                        "start": start_date,
                        "end": end_date,
                        "attempt": 1,
                    }
                )

        logger.info(f"✓ 共享任务池构建完成，共生成 {len(tasks)} 个唯一任务。")
        return tasks

    async def execute_task(
        self,
        page: Page,
        task: Dict[str, Any],
        list_index: int,
    ) -> bool:
        """执行一个独立日期区块；普通失败返回 False，致命页面异常向外抛出。"""
        task_card_index = task["card"]
        date_chunks = [(task["start"], task["end"])]
        worker_id = f"页面-{list_index + 1}"
        logger.info(
            f"Worker-{worker_id} 开始处理任务 {task['task_id']} "
            f"（第 {task['attempt']} 次尝试）"
        )
        
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
            fatal_reason = self._fatal_page_error_reason(e)
            if fatal_reason:
                logger.error(f"Worker-{worker_id} 初始化期间检测到致命页面异常（{fatal_reason}），停止该Worker: {e}")
                raise
            else:
                logger.error(f"Worker-{worker_id} 任务页面初始化失败: {e}")
            await asyncio.sleep(5)
            return False
            
        # execute_task 每次只接收一个日期区块，保留单层循环以复用原业务流程。
        for start_date, end_date in date_chunks:
            task_submitted = False
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
                    if self._fatal_page_error_reason(e):
                        raise
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
                            if self._fatal_page_error_reason(e):
                                raise
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
                    if self._fatal_page_error_reason(e):
                        raise
                    logger.error(f"Worker-{worker_id} 二级弹窗处理阶段发生未知异常: {e}")

                if not click_success:
                    logger.error(f"Worker-{worker_id} 全店补齐未成功提交，当前区间不进入心跳判定。")
                    try:
                        await self._restore_primary_state(page, worker_id)
                    except Exception as cleanup_error:
                        if self._fatal_page_error_reason(cleanup_error):
                            raise
                        logger.warning(f"Worker-{worker_id} 提交失败后清理弹窗异常: {cleanup_error}")
                    logger.warning(f"Worker-{worker_id} 当前任务 {start_date} 至 {end_date} 记为失败。")
                    return False

                task_submitted = True
                
                # 4. 开始完工判定（交由 GC 守护进程管理蓝页，此处只需判定本页弹窗状态）
                completed_normally = await self.wait_for_completion_or_heartbeat(page, worker_id)
                if completed_normally:
                    logger.info(f"Worker-{worker_id} 成功跑完任务: {start_date} 至 {end_date}")
                else:
                    logger.warning(f"Worker-{worker_id} 当前区间判定为卡死并已清理: {start_date} 至 {end_date}")
                return completed_normally
                
            except Exception as e:
                fatal_reason = self._fatal_page_error_reason(e)
                if fatal_reason:
                    current_state = (
                        "已经提交【全店补齐】，但最终结果未知"
                        if task_submitted
                        else "尚未完成【全店补齐】提交"
                    )
                    logger.error(
                        f"Worker-{worker_id} 检测到致命页面异常（{fatal_reason}），停止该Worker: {e}"
                    )
                    logger.warning(
                        f"Worker-{worker_id} 当前区间 {start_date} 至 {end_date} {current_state}；"
                        "当前页面不再领取新任务。"
                    )
                    raise
                    
                logger.error(f"Worker-{worker_id} 在执行 {start_date} 至 {end_date} 期间发生错误: {e}")
                logger.warning(f"Worker-{worker_id} 当前任务记为失败。")
                await asyncio.sleep(5)
                return False

        # 无缺失数据时会走到这里，视为任务正常完成。
        logger.info(f"Worker-{worker_id} 任务 {task['task_id']} 无缺失数据，账本记为成功。")
        return True

    async def worker(
        self,
        page: Page,
        task_queue: asyncio.Queue,
        ledger: TaskLedger,
        list_index: int,
        round_name: str,
    ) -> bool:
        """持续消费共享队列；返回值表示当前页面能否继续用于下一轮。"""
        worker_id = f"页面-{list_index + 1}"
        logger.info(
            f"Worker-{worker_id} 启动{round_name}，绑定页面: {page.url[-25:]}"
        )

        while True:
            try:
                task = task_queue.get_nowait()
            except asyncio.QueueEmpty:
                logger.info(f"Worker-{worker_id} 已完成{round_name}的任务领取。")
                return True

            fatal_error = False
            try:
                success = await self.execute_task(page, task, list_index)
            except Exception as error:
                fatal_reason = self._fatal_page_error_reason(error)
                success = False
                if fatal_reason:
                    fatal_error = True
                    logger.error(f"Worker-{worker_id} 因{fatal_reason}停止领取新任务。")
                else:
                    logger.error(
                        f"Worker-{worker_id} 执行任务时发生未分类异常，"
                        f"当前任务记为失败: {error}"
                    )

            try:
                await ledger.record(task, success)
            finally:
                task_queue.task_done()

            if fatal_error:
                return False

    async def _run_task_round(
        self,
        tasks: List[Dict[str, Any]],
        worker_pages: List[Page],
        ledger: TaskLedger,
        round_name: str,
    ) -> List[Page]:
        """让全部可用页面并发消费一轮预先装满的共享任务队列。"""
        if not tasks:
            logger.info(f"{round_name}没有需要执行的任务。")
            return worker_pages

        task_queue: asyncio.Queue = asyncio.Queue()
        for task in tasks:
            task_queue.put_nowait(task)

        logger.info(
            f"\n{'=' * 40}\n{round_name}开始：{len(tasks)} 个任务，"
            f"{len(worker_pages)} 个可用Worker\n{'=' * 40}"
        )

        if not worker_pages:
            logger.error(f"{round_name}没有可用Worker，本轮任务全部记为失败。")
            while True:
                try:
                    task = task_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await ledger.record(task, False)
                finally:
                    task_queue.task_done()
            return []

        worker_results = await asyncio.gather(
            *[
                self.worker(page, task_queue, ledger, index, round_name)
                for index, page in enumerate(worker_pages)
            ]
        )
        healthy_pages = [
            page
            for page, is_healthy in zip(worker_pages, worker_results)
            if is_healthy
        ]

        # 正常情况下健康Worker会取完全部任务；这里只兜底处理全部页面都崩溃的情况。
        unprocessed_count = 0
        while True:
            try:
                task = task_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await ledger.record(task, False)
                unprocessed_count += 1
            finally:
                task_queue.task_done()

        if unprocessed_count:
            logger.warning(
                f"{round_name}结束时已无可用Worker，剩余 {unprocessed_count} 个"
                "未执行任务已记为失败。"
            )

        logger.info(
            f"{round_name}结束：仍有 {len(healthy_pages)} 个Worker可用于后续调度。"
        )
        return healthy_pages

    @staticmethod
    def _log_summary(summary: Dict[str, int]) -> None:
        """输出本轮首次执行、总体重试和最终完成情况。"""
        logger.info(
            "\n任务执行汇总：\n"
            f"  本轮任务总数：{summary['total']}\n"
            f"  首次执行成功：{summary['first_success']}\n"
            f"  首次执行失败：{summary['first_failed']}\n"
            f"  进入总体重试：{summary['retry_total']}\n"
            f"  重试成功：{summary['retry_success']}\n"
            f"  重试仍失败：{summary['retry_failed']}\n"
            f"  最终完成：{summary['final_success']}\n"
            f"  最终失败：{summary['final_failed']}"
        )

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

            if not isinstance(tasks_config, list) or not all(
                isinstance(config, dict) for config in tasks_config
            ):
                logger.error("tasks_config 必须是 list[dict]，引擎停止运行。")
                return

            initial_tasks = self.build_tasks(tasks_config)
            if not initial_tasks:
                logger.warning("配置没有生成任何有效日期任务，调度结束。")
                return

            ledger = TaskLedger(runtime_dir / "backfill_results.jsonl")
            try:
                await ledger.reset()
            except Exception as error:
                logger.error(f"无法创建或覆盖任务账本，调度停止: {error}")
                return

            logger.info(
                f"本轮任务账本已重置: {ledger.path}；日志继续追加到: {log_path}"
            )

            # 每个数据中心 Worker 都有独立的错误提示回收器，不与业务任务绑定。
            error_toast_monitors = [
                asyncio.create_task(
                    self._monitor_worker_error_toasts(page, f"页面-{index + 1}")
                )
                for index, page in enumerate(worker_pages)
            ]

            try:
                # 第一轮：所有页面从同一个共享任务池动态领取日期区块。
                healthy_pages = await self._run_task_round(
                    initial_tasks,
                    worker_pages,
                    ledger,
                    "首次执行",
                )

                # 第二轮：必须从 JSONL 读取第一轮失败项，只进行一次总体重试。
                retry_tasks = await ledger.failed_tasks(attempt=1)
                if retry_tasks:
                    logger.warning(
                        f"首次执行结束，从任务账本读取到 {len(retry_tasks)} 个失败任务，"
                        "开始唯一一次总体重试。"
                    )
                    await self._run_task_round(
                        retry_tasks,
                        healthy_pages,
                        ledger,
                        "总体重试",
                    )
                else:
                    logger.info("首次执行没有失败任务，无需总体重试。")

                summary = await ledger.summary(total_tasks=len(initial_tasks))
                self._log_summary(summary)
            finally:
                await self._stop_error_toast_monitors(error_toast_monitors)

if __name__ == "__main__":
    bite_id = '4626a1f1fadb4ac4aab182d93469147f'
    # 任务配置列表：每个字典描述一个待拆分的卡片日期范围。
    # 所有拆分后的日期区块进入共享任务池，由可用标签页动态领取。
    tasks_config = [
        {"card": 3, "start": "2025-07-01", "end": "2025-12-31", "chunk_days": 1},
        {"card": 5, "start": "2025-09-01", "end": "2025-09-30", "chunk_days": 1},
        {"card": 5, "start": "2025-10-01", "end": "2025-10-31", "chunk_days": 1},
        {"card": 5, "start": "2025-11-01", "end": "2025-11-30", "chunk_days": 1},
        {"card": 5, "start": "2025-12-01", "end": "2025-12-31", "chunk_days": 1},
    ]
    
    engine = BackfillEngine(bite_id)
    asyncio.run(engine.run(tasks_config))
