#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RPA 历史数据自动化补采调度引擎 (Playwright Async 版)
基于比特浏览器架构，支持多标签页并发任务分配与心跳防卡死监控。
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Dict, Optional, List, Tuple, TypeVar

import requests
from dotenv import load_dotenv
from playwright.async_api import (
    BrowserContext,
    ElementHandle,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from task_ledger import TaskLedger


T = TypeVar("T")


class WorkerUnresponsiveError(RuntimeError):
    """页面仍连接但已无法在限定时间内响应，当前Worker必须退出。"""


class TaskPageInitializationError(RuntimeError):
    """单次任务页面初始化失败；连续发生时触发Worker熔断。"""


class MissingDataRenderError(RuntimeError):
    """后端检测已触发，但顶部缺失统计文本始终未完成渲染。"""

# 配置全局日志（同时输出到控制台和本地文件）
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("BackfillEngine")
logger.setLevel(logging.INFO)

# 1. 默认输出到控制台；daily-mode 可在导入本模块前通过环境变量关闭。
console_logging_enabled = os.environ.get("RPA_CONSOLE_LOGGING", "1").lower() not in {
    "0",
    "false",
    "no",
}
if console_logging_enabled and sys.stderr is not None:
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    logger.addHandler(console_handler)

# 2. 输出到本地文件：打包后位于 exe 同目录，源码运行时位于脚本同目录。
runtime_dir = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
log_path = runtime_dir / os.environ.get("RPA_LOG_FILENAME", "backfill_run.log")
try:
    file_handler = logging.FileHandler(log_path, encoding='utf-8')
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)
except Exception as e:
    print(f"无法创建日志文件 {log_path}: {e}")



class BackfillEngine:
    """历史数据补采引擎"""

    def __init__(
        self,
        bite_id: str,
        gc_page_url_markers: List[str],
    ):
        self.bt_url = 'http://127.0.0.1:54345'
        self.bite_id = bite_id
        # 心跳静默判定机制超时时间（秒）
        self.silent_timeout_seconds = 120
        # Context 级业务执行页 GC 比 Worker 多保留 60 秒观察窗口。
        self.gc_silent_timeout_seconds = 180
        # 程序收尾时覆盖 120/180 秒判定之间的窗口，并额外预留 5 秒调度余量。
        self.gc_shutdown_grace_seconds = (
            self.gc_silent_timeout_seconds - self.silent_timeout_seconds + 5
        )
        # 纳入 Context 级 GC 的业务执行页 URL 标记。其他平台只有在使用
        # 相同心跳 DOM 协议时，才可以直接追加到这个元组。
        if not gc_page_url_markers or not all(
            isinstance(marker, str) and marker.strip()
            for marker in gc_page_url_markers
        ):
            raise ValueError("gc_page_url_markers 必须是非空字符串列表")
        self.gc_page_url_markers = tuple(
            marker.strip() for marker in gc_page_url_markers
        )
        # 红色错误提示保留时间：既给人工观察留出窗口，也避免长期堆积遮挡页面。
        self.error_toast_grace_seconds = 30
        # 本应快速完成的页面状态查询，由asyncio从Playwright外层施加硬超时。
        self.page_probe_timeout_seconds = 10
        # 普通初始化异常允许短暂恢复，连续达到阈值后隔离当前Worker。
        self.max_consecutive_initialization_failures = 3
        self._error_toast_close_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _fatal_page_error_reason(error: Exception) -> Optional[str]:
        """识别页面崩溃、关闭或浏览器断连等无法继续执行的致命异常。"""
        if isinstance(error, WorkerUnresponsiveError):
            return str(error)

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

    async def _await_page_operation(
        self,
        operation: Awaitable[T],
        worker_id: str,
        operation_name: str,
        timeout_seconds: Optional[float] = None,
    ) -> T:
        """给可能缺少协议级超时的短页面操作增加asyncio硬超时。"""
        timeout = timeout_seconds or self.page_probe_timeout_seconds
        try:
            return await asyncio.wait_for(operation, timeout=timeout)
        except asyncio.TimeoutError as error:
            raise WorkerUnresponsiveError(
                f"Worker-{worker_id} {operation_name}超过 {timeout:g} 秒无响应"
            ) from error

    async def _locator_is_visible(
        self,
        locator: Locator,
        worker_id: str,
        locator_name: str,
    ) -> bool:
        """在硬超时保护下判断Locator是否存在且可见。"""
        count = await self._await_page_operation(
            locator.count(),
            worker_id,
            f"查询{locator_name}数量",
        )
        if count == 0:
            return False
        return await self._await_page_operation(
            locator.is_visible(),
            worker_id,
            f"查询{locator_name}可见性",
        )

    async def _assert_page_healthy(self, page: Page, worker_id: str) -> None:
        """通过一次受硬超时保护的JS往返确认页面渲染事件循环仍能响应。"""
        if page.is_closed():
            raise WorkerUnresponsiveError(f"Worker-{worker_id} 页面已经关闭")

        ready_state = await self._await_page_operation(
            page.evaluate("() => document.readyState"),
            worker_id,
            "页面健康探测",
        )
        if ready_state not in {"loading", "interactive", "complete"}:
            raise WorkerUnresponsiveError(
                f"Worker-{worker_id} 页面健康探测返回异常状态: {ready_state!r}"
            )

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
                if self._is_gc_managed_page_url(page.url):
                    # 确认为受 GC 管理的业务执行页，部署监控协程。
                    url_suffix = page.url[-25:] if len(page.url) > 25 else page.url
                    logger.info(f"[GC Daemon] 发现业务执行网页，开始后台监控: {url_suffix}")
                    asyncio.create_task(self._monitor_and_gc_page(page))
                    return
                await asyncio.sleep(1)
        except Exception:
            pass

    def _on_new_page(self, page: Page):
        """拦截浏览器新建标签页的事件"""
        asyncio.create_task(self._delayed_check(page))

    def _is_gc_managed_page_url(self, url: str) -> bool:
        """判断 URL 是否属于应由 Context 级 GC 管理的业务执行页面。"""
        normalized_url = url.lower()
        return any(
            marker.lower() in normalized_url
            for marker in self.gc_page_url_markers
        )

    def _remaining_gc_pages(self, context: BrowserContext) -> List[Page]:
        """返回 Context 中尚未关闭、且符合 GC URL 规则的业务执行页面。"""
        return [
            page
            for page in context.pages
            if not page.is_closed() and self._is_gc_managed_page_url(page.url)
        ]

    async def _cleanup_remaining_gc_pages(
        self,
        context: BrowserContext,
    ) -> None:
        """在主调度结束后给 GC 留出窗口，并兜底关闭仍残留的业务执行页。"""
        remaining_pages = self._remaining_gc_pages(context)
        if not remaining_pages:
            return

        logger.info(
            f"程序收尾时仍有 {len(remaining_pages)} 个业务执行页面；"
            f"等待 {self.gc_shutdown_grace_seconds} 秒交由 GC 自然回收。"
        )
        await asyncio.sleep(self.gc_shutdown_grace_seconds)

        remaining_pages = self._remaining_gc_pages(context)
        if not remaining_pages:
            logger.info("程序收尾宽限期内，残留业务执行页面已全部自然关闭。")
            return

        logger.warning(
            f"程序收尾宽限期结束后仍有 {len(remaining_pages)} 个业务执行页面，"
            "执行兜底关闭。"
        )
        close_results = await asyncio.gather(
            *(page.close() for page in remaining_pages),
            return_exceptions=True,
        )
        failed_count = sum(
            isinstance(result, BaseException) for result in close_results
        )
        if failed_count:
            logger.warning(f"程序收尾时有 {failed_count} 个业务执行页面关闭失败。")
        else:
            logger.info("程序收尾时的残留业务执行页面已全部关闭。")

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
                    timeout=self.gc_silent_timeout_seconds * 1000,
                )
            except PlaywrightTimeoutError:
                # 180 秒内无任何心跳，判定为残留僵尸网页
                if not page.is_closed():
                    logger.warning(
                        f"[GC Daemon] 业务执行网页 {url_suffix} 超过 "
                        f"{self.gc_silent_timeout_seconds} 秒无心跳，"
                        "判定为残留任务，执行强制关闭。"
                    )
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
                    logger.warning(f"[GC Daemon] 业务执行网页 {url_suffix} 当前心跳弹窗节点超过 15 秒仍未隐藏，判定页面状态异常，执行强制关闭。")
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
        if not await self._locator_is_visible(
            container, worker_id, layer_name
        ):
            return False

        close_button = container.locator("i.el-icon-close")
        close_count = await self._await_page_operation(
            close_button.count(),
            worker_id,
            f"查询{layer_name}关闭按钮数量",
        )
        if close_count != 1:
            raise RuntimeError(
                f"Worker-{worker_id} {layer_name}内部预期 1 个关闭按钮，实际找到 {close_count} 个"
            )

        logger.info(f"Worker-{worker_id} 正在关闭{layer_name}...")
        try:
            await close_button.click(timeout=5000)
        except PlaywrightTimeoutError:
            # 仅对已经精确限定在弹窗内部的关闭按钮使用 DOM 点击，避免遮挡层
            # 导致 Playwright 命中测试永久失败；业务按钮仍保留真实点击保护。
            logger.warning(
                f"Worker-{worker_id} {layer_name}关闭按钮无法完成常规点击，"
                "改用精准 DOM 点击。"
            )
            await self._await_page_operation(
                close_button.evaluate("node => node.click()"),
                worker_id,
                f"精准点击{layer_name}关闭按钮",
            )

        # ElementUI Drawer 关闭后会保留在 DOM 中并变成零尺寸，hidden 可同时兼容隐藏和移除。
        try:
            await container.wait_for(state="hidden", timeout=timeout_ms)
        except PlaywrightTimeoutError as error:
            # 区分“页面彻底不响应”和“页面仍响应但关闭事件未生效”。后一种情况
            # 同样无法安全复用当前 Worker，因此也应退出任务池。
            await self._assert_page_healthy(container.page, worker_id)
            if not await self._locator_is_visible(container, worker_id, layer_name):
                logger.info(f"Worker-{worker_id} {layer_name}已在超时边界完成关闭。")
                return True
            raise WorkerUnresponsiveError(
                f"Worker-{worker_id} {layer_name}关闭指令已发出，但弹窗仍未隐藏"
            ) from error

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
        await primary_drawer.wait_for(
            state="visible",
            timeout=30000,
        )
        # trial 只做完整可点击性检查，不触发实际检测。
        try:
            await primary_drawer.locator("#checkbutn").click(trial=True, timeout=5000)
        except PlaywrightTimeoutError as error:
            await self._assert_page_healthy(page, worker_id)
            raise WorkerUnresponsiveError(
                f"Worker-{worker_id} 恢复一级弹窗后【启动检测】按钮仍不可点击"
            ) from error

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

            input_count = await self._await_page_operation(
                inputs.count(),
                worker_id,
                "查询日期输入框数量",
            )
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

    async def _detect_missing_data(
        self,
        page: Page,
        primary_drawer: Locator,
        worker_id: str,
        start_date: str,
        end_date: str,
        phase: str,
    ) -> Optional[bool]:
        """重新请求后端检测缺失量；True=有缺失，False=无缺失，None=结果不确定。"""
        start_btn = primary_drawer.locator("#checkbutn")
        result_list = primary_drawer.locator(".testContent_list")
        missing_span = primary_drawer.locator(
            "div.testContent > div:nth-child(2) > span:nth-child(1)"
        )

        for detection_attempt in range(1, 4):
            try:
                logger.info(
                    f"Worker-{worker_id} {phase}第 {detection_attempt}/3 次请求后端检测："
                    f"{start_date} 至 {end_date}。"
                )
                await start_btn.click(timeout=30000)

                try:
                    await result_list.wait_for(state="visible", timeout=45000)
                except PlaywrightTimeoutError:
                    logger.warning(
                        f"Worker-{worker_id} {phase}等待查询结果容器超时，"
                        "继续读取缺失统计。"
                    )

                # 检测结果区可见后，给顶部缺失统计文本一个短暂渲染缓冲。
                await page.wait_for_timeout(1000)
                for read_attempt, retry_delay_ms in enumerate(
                    (0, 2000, 4000), start=1
                ):
                    if retry_delay_ms:
                        logger.warning(
                            f"Worker-{worker_id} {phase}统计文本仍为渲染占位符，"
                            f"等待 {retry_delay_ms // 1000} 秒后进行第 {read_attempt}/3 次读取。"
                        )
                        await page.wait_for_timeout(retry_delay_ms)

                    await missing_span.wait_for(state="attached", timeout=30000)
                    missing_text = (await missing_span.inner_text()).strip()
                    if missing_text != "：表示缺失数据":
                        break
                else:
                    raise MissingDataRenderError(
                        f"Worker-{worker_id} {phase}连续 3 次读取均为统计文本渲染占位符。"
                    )
            except MissingDataRenderError:
                raise
            except Exception as error:
                if self._fatal_page_error_reason(error):
                    raise
                logger.warning(
                    f"Worker-{worker_id} {phase}第 {detection_attempt}/3 次检测异常: "
                    f"{error}"
                )
                continue

            match = re.search(r"-?\d+", missing_text)
            if not match:
                logger.info(
                    f"Worker-{worker_id} {phase}统计文本 [{missing_text}] 不含数字，"
                    "确认当前日期无缺失数据。"
                )
                return False

            missing_count = int(match.group())
            if missing_count > 0:
                log_method = logger.warning if phase == "终态复检" else logger.info
                log_method(
                    f"Worker-{worker_id} {phase}统计文本 [{missing_text}]，"
                    f"确认仍有 {missing_count} 条缺失数据。"
                )
                return True

            logger.warning(
                f"Worker-{worker_id} {phase}统计文本 [{missing_text}] 显示 0 或负数，"
                "视为前端渲染异常并重新检测。"
            )

        logger.error(
            f"Worker-{worker_id} {phase}连续 3 次仍无法获得可信缺失量。"
        )
        return None

    async def wait_for_completion_or_heartbeat(
        self,
        page: Page,
        worker_id: str,
        start_date: str,
        end_date: str,
    ) -> bool:
        """
        心跳静默只触发终态复检；最终以重新请求后端得到的缺失量判定成功。
        """
        toast_selector = ".el-message__content:has-text('同步成功')"
        silent_timeout_ms = self.silent_timeout_seconds * 1000 
        
        logger.info(
            f"Worker-{worker_id} 开始静默监听（超过 "
            f"{self.silent_timeout_seconds} 秒无心跳则进入后端终态复检）..."
        )
        
        while True:
            try:
                # 保存当前这一个心跳节点。后续只跟踪它，不让新出现的同类提示替换等待目标。
                toast_handle = await page.wait_for_selector(
                    toast_selector,
                    state="attached",
                    timeout=silent_timeout_ms,
                )
            except PlaywrightTimeoutError:
                logger.info(
                    f"Worker-{worker_id} 超过 {self.silent_timeout_seconds} 秒无新心跳，"
                    "开始终态复检。"
                )
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
                
        logger.info(
            f"Worker-{worker_id} 心跳停止，恢复一级弹窗并重新请求后端确认缺失量。"
        )
        await self._restore_primary_state(page, worker_id)
        await self.inject_dates(page, start_date, end_date, worker_id)
        terminal_missing = await self._detect_missing_data(
            page,
            self._primary_drawer(page),
            worker_id,
            start_date,
            end_date,
            "终态复检",
        )

        if terminal_missing is False:
            logger.info(
                f"Worker-{worker_id} 终态复检确认无缺失数据，当前任务成功。"
            )
            return True
        if terminal_missing is True:
            logger.warning(
                f"Worker-{worker_id} 终态复检仍有缺失数据，当前任务失败。"
            )
            return False

        logger.warning(
            f"Worker-{worker_id} 终态复检结果不确定，不写入成功结果。"
        )
        return False

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
            await primary_drawer.wait_for(
                state="visible",
                timeout=30000,
            )
            await primary_drawer.locator("#checkbutn").click(trial=True, timeout=5000)
            logger.info(f"Worker-{worker_id} 初始化完成，已成功进入补采专属弹窗！")
            
            # 不需要记录基准线，依靠锚点即可
        except Exception as e:
            fatal_reason = self._fatal_page_error_reason(e)
            if fatal_reason:
                logger.error(f"Worker-{worker_id} 初始化期间检测到致命页面异常（{fatal_reason}），停止该Worker: {e}")
                raise
            logger.error(f"Worker-{worker_id} 任务页面初始化失败: {e}")
            await asyncio.sleep(5)
            raise TaskPageInitializationError(
                f"Worker-{worker_id} 任务页面初始化失败"
            ) from e
            
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

                # 2. 请求后端检测缺失量；不确定时仍进入补齐流程兜底。
                detection_result = await self._detect_missing_data(
                    page,
                    primary_drawer,
                    worker_id,
                    start_date,
                    end_date,
                    "首次检测",
                )

                if detection_result is False:
                    continue
                if detection_result is None:
                    logger.warning(
                        f"Worker-{worker_id} 首次检测结果不确定，"
                        "将进入补齐流程兜底。"
                    )

                # --- 既然有缺失数据（或探测异常兜底），则走后续补齐流程 ---
                logger.info(f"Worker-{worker_id} 准备点击一级补齐数据按钮...")
                backfill_btn = primary_drawer.locator("span.lostDataBtn")
                await backfill_btn.wait_for(
                    state="visible",
                    timeout=30000,
                )
                await backfill_btn.click(
                    timeout=30000
                )
                
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
                            await whole_store_btn.wait_for(
                                state="visible",
                                timeout=30000,
                            )
                            # 依赖 Playwright 原生拦截检测机制，若有遮挡则主动抛出异常进入恢复流
                            await whole_store_btn.click(
                                timeout=30000
                            )
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
                                await backfill_btn.click(
                                    timeout=30000
                                )
                                
                                # 3. 等待二级弹窗重新渲染
                                await secondary_drawer.wait_for(
                                    state="visible",
                                    timeout=30000,
                                )
                        
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
                
                # 4. 心跳静默后重新请求后端检测，以真实缺失量决定账本结果。
                completed_normally = await self.wait_for_completion_or_heartbeat(
                    page,
                    worker_id,
                    start_date,
                    end_date,
                )
                if completed_normally:
                    logger.info(f"Worker-{worker_id} 成功跑完任务: {start_date} 至 {end_date}")
                else:
                    logger.warning(
                        f"Worker-{worker_id} 当前区间终态复检未通过: "
                        f"{start_date} 至 {end_date}"
                    )
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
        consecutive_initialization_failures = 0

        while True:
            try:
                task = task_queue.get_nowait()
            except asyncio.QueueEmpty:
                logger.info(f"Worker-{worker_id} 已完成{round_name}的任务领取。")
                return True

            fatal_error = False
            try:
                success = await self.execute_task(page, task, list_index)
                # execute_task 能进入业务流程（无论业务最终成功与否），说明页面初始化正常。
                consecutive_initialization_failures = 0
            except TaskPageInitializationError as error:
                success = False
                consecutive_initialization_failures += 1
                logger.warning(
                    f"Worker-{worker_id} 连续初始化失败 "
                    f"{consecutive_initialization_failures}/"
                    f"{self.max_consecutive_initialization_failures}: {error}"
                )
                if (
                    consecutive_initialization_failures
                    >= self.max_consecutive_initialization_failures
                ):
                    fatal_error = True
                    logger.error(
                        f"Worker-{worker_id} 已达到连续初始化失败阈值，"
                        "触发熔断并停止领取新任务。"
                    )
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
            ],
            return_exceptions=True,
        )
        healthy_pages = []
        for index, (page, result) in enumerate(zip(worker_pages, worker_results)):
            if result is True:
                healthy_pages.append(page)
            elif isinstance(result, BaseException):
                logger.error(
                    f"Worker-页面-{index + 1} 协程异常退出，已从后续轮次隔离: {result}"
                )

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
                await self._cleanup_remaining_gc_pages(context)


def _load_json_list_env(name: str) -> List[Any]:
    """读取值为 JSON 数组的环境变量，并给出可定位的配置错误。"""
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        raise ValueError(f".env 缺少必填配置 {name}")

    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ValueError(f".env 中的 {name} 不是有效 JSON 数组: {error}") from error

    if not isinstance(value, list):
        raise ValueError(f".env 中的 {name} 必须是 JSON 数组")
    return value


def load_runtime_config() -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """从源码或 exe 同目录的 .env 加载本次运行配置。"""
    env_path = runtime_dir / ".env"
    if not env_path.exists():
        raise FileNotFoundError(
            f"未找到运行配置 {env_path}；请复制 .env.example 为 .env 后填写。"
        )

    load_dotenv(env_path)
    bite_id = (os.getenv("BITE_ID") or "").strip()
    if not bite_id:
        raise ValueError(".env 缺少必填配置 BITE_ID")

    tasks_config_raw = _load_json_list_env("TASKS_CONFIG")
    if not tasks_config_raw or not all(
        isinstance(config, dict) for config in tasks_config_raw
    ):
        raise ValueError("TASKS_CONFIG 必须是非空的 JSON 对象数组")

    markers_raw = _load_json_list_env("GC_PAGE_URL_MARKERS")
    if not markers_raw or not all(
        isinstance(marker, str) and marker.strip() for marker in markers_raw
    ):
        raise ValueError("GC_PAGE_URL_MARKERS 必须是非空字符串数组")

    return bite_id, tasks_config_raw, markers_raw


if __name__ == "__main__":
    try:
        bite_id, tasks_config, gc_page_url_markers = load_runtime_config()
    except (OSError, ValueError) as error:
        logger.error(f"运行配置加载失败: {error}")
        sys.exit(1)

    engine = BackfillEngine(
        bite_id,
        gc_page_url_markers=gc_page_url_markers,
    )
    asyncio.run(engine.run(tasks_config))
