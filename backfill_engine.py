#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RPA 历史数据自动化补采调度引擎 (Playwright Async 版)
支持连接比特浏览器或已开启远程调试的 Chromium 浏览器，
并提供多标签页并发任务分配与心跳防卡死监控。
"""

import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Dict, Optional, List, Tuple, TypeVar

from dotenv import load_dotenv
from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from browser_connector import (
    BitBrowserConnector,
    BrowserConnector,
    ExternalCdpConnector,
    get_cdp_browser_identity,
    normalize_cdp_address,
)
from github_info import GIT_SHA
from slider_motion_tools import solve_closed_shadow_slider
from task_ledger import TaskLedger


T = TypeVar("T")


class WorkerUnresponsiveError(RuntimeError):
    """页面仍连接但已无法在限定时间内响应，当前Worker必须退出。"""


class PageProbeTimeoutError(RuntimeError):
    """单次页面探针超时；当前任务失败，但不足以判定Worker失效。"""


class TaskPageInitializationError(RuntimeError):
    """单次任务页面初始化失败；连续发生时触发Worker熔断。"""


class MissingDataRenderError(RuntimeError):
    """后端检测已触发，但顶部缺失统计文本始终未完成渲染。"""


SESSION_END_REASON_PRIORITY = {
    "tasks_completed": 0,
    "lifetime_expired": 10,
    "no_workers": 20,
    "connection_lost": 30,
    "session_error": 40,
    "ledger_error": 50,
}


@dataclass
class CdpSessionControl:
    """单次 CDP 会话共享的停止信号、软截止时间和最终退出原因。"""

    deadline: float
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    end_reason: Optional[str] = None

    def request_stop(self, reason: str) -> None:
        current_priority = SESSION_END_REASON_PRIORITY.get(
            self.end_reason or "tasks_completed",
            -1,
        )
        new_priority = SESSION_END_REASON_PRIORITY.get(reason, -1)
        if self.end_reason is None or new_priority > current_priority:
            self.end_reason = reason
        self.stop_event.set()


@dataclass(frozen=True)
class CdpSessionResult:
    """一次 CDP 会话结束后交给 Backfill 顶层调度器的结果。"""

    reason: str
    ledger_ready: bool
    worker_count: int = 0
    queue_completed: bool = False

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
        bite_id: Optional[str],
        gc_page_url_markers: List[str],
        *,
        browser_connector: Optional[BrowserConnector] = None,
        worker_heartbeat_silence_seconds: int = 120,
        business_heartbeat_silence_seconds: int = 180,
        max_attempts: int = 5,
        cdp_session_lifetime_hours: float = 3.5,
        max_cdp_rebuilds: int = 5,
    ):
        self.bt_url = 'http://127.0.0.1:54345'
        self.bite_id = bite_id
        if browser_connector is None:
            if not bite_id:
                raise ValueError("使用比特浏览器连接器时 bite_id 不能为空")
            browser_connector = BitBrowserConnector(bite_id, self.bt_url)
        self.browser_connector = browser_connector

        if worker_heartbeat_silence_seconds <= 0:
            raise ValueError("worker_heartbeat_silence_seconds 必须大于 0")
        if business_heartbeat_silence_seconds <= worker_heartbeat_silence_seconds:
            raise ValueError(
                "business_heartbeat_silence_seconds 必须大于 "
                "worker_heartbeat_silence_seconds"
            )
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须大于 0")
        if (
            not math.isfinite(cdp_session_lifetime_hours)
            or cdp_session_lifetime_hours <= 0
        ):
            raise ValueError("cdp_session_lifetime_hours 必须大于 0")
        if max_cdp_rebuilds < 0:
            raise ValueError("max_cdp_rebuilds 不能小于 0")
        # 心跳静默判定机制超时时间（秒）
        self.silent_timeout_seconds = worker_heartbeat_silence_seconds
        # Context 级业务执行页 GC 必须比 Worker 保留更长的观察窗口。
        self.gc_silent_timeout_seconds = business_heartbeat_silence_seconds
        # 程序收尾时覆盖 Worker/GC 判定之间的窗口，并额外预留 5 秒调度余量。
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
        # 红色错误提示短暂保留后自动关闭，避免堆积遮挡后续业务按钮。
        self.error_toast_grace_seconds = 2
        # 一级【启动检测】按钮可能正等待错误提示完成退出动画，适当延长可点击性检查。
        self.primary_actionability_timeout_ms = 15000
        # 本应快速完成的页面状态查询，由asyncio从Playwright外层施加硬超时。
        self.page_probe_timeout_seconds = 20
        # 探针超时通常来自浏览器高负载，冷却后保留Worker并重试队列任务。
        self.page_probe_cooldown_seconds = 10
        # 普通初始化异常允许短暂恢复，连续达到阈值后隔离当前Worker。
        self.initialization_failure_cooldown_seconds = 20
        self.max_consecutive_initialization_failures = 5
        self.max_attempts = max_attempts
        self.cdp_session_lifetime_seconds = cdp_session_lifetime_hours * 3600
        self.cdp_session_lifetime_hours = cdp_session_lifetime_hours
        self.max_cdp_rebuilds = max_cdp_rebuilds
        self._error_toast_close_tasks: set[asyncio.Task] = set()
        self._gc_background_tasks: set[asyncio.Task] = set()

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

    @staticmethod
    def _is_driver_connection_error(error: BaseException) -> bool:
        """识别可以明确指向 Playwright Driver/CDP 连接失效的异常。"""
        error_msg = str(error).lower()
        return any(
            marker in error_msg
            for marker in (
                "connection closed while reading from the driver",
                "playwright connection closed",
                "playwright driver connection closed",
                "the driver connection has been closed",
            )
        )

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
            raise PageProbeTimeoutError(
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

    async def _delayed_check(self, page: Page):
        """延迟检测新网页 URL 并部署后台监控任务"""
        managed_page = False
        url_suffix = "<unknown>"
        try:
            # 等待最多 10 秒，让网页跳转到真实的 URL
            for _ in range(10):
                if page.is_closed():
                    return
                current_url = page.url
                url_suffix = (
                    current_url[-25:] if len(current_url) > 25 else current_url
                )
                if self._is_slider_page_url(current_url):
                    logger.info(
                        f"[Slider] 发现滑块验证网页，开始处理: {url_suffix}"
                    )
                    try:
                        solved = await self._solve_slider_page(page)
                    except Exception as error:
                        logger.exception(
                            f"[Slider] 滑块验证处理异常，仍将交给 GC 监控: "
                            f"{error}"
                        )
                        solved = False

                    if solved:
                        logger.info(f"[Slider] 滑块验证处理完成: {url_suffix}")
                    else:
                        logger.warning(
                            f"[Slider] 滑块验证未通过，仍将交给 GC 监控: "
                            f"{url_suffix}"
                        )
                    if page.is_closed():
                        return

                    # 滑块通过后页面内容会变化，但 URL 仍可能保留原前缀；
                    # 无论验证结果如何，都按业务执行页部署心跳和静默回收。
                    managed_page = True
                    logger.info(
                        f"[GC Daemon] 滑块验证网页开始后台监控: {url_suffix}"
                    )
                    monitor_task = asyncio.create_task(
                        self._monitor_and_gc_page(page)
                    )
                    self._track_gc_background_task(monitor_task)
                    return
                if self._is_gc_managed_page_url(current_url):
                    # 确认为受 GC 管理的业务执行页，部署监控协程。
                    managed_page = True
                    logger.info(f"[GC Daemon] 发现业务执行网页，开始后台监控: {url_suffix}")
                    monitor_task = asyncio.create_task(
                        self._monitor_and_gc_page(page)
                    )
                    self._track_gc_background_task(monitor_task)
                    return
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if page.is_closed():
                logger.debug(
                    f"[GC Daemon] 新网页 {url_suffix} 在延迟识别期间已关闭。"
                )
                return
            logger.exception(
                f"[GC Daemon] 新网页 {url_suffix} 延迟识别发生异常: "
                f"{type(error).__name__}: {error}"
            )
            # 只有已经确认属于 GC 管理范围的业务页才关闭，避免误伤未知页面。
            if managed_page:
                await self._close_gc_page_after_exception(
                    page,
                    url_suffix,
                    "延迟识别",
                )

    def _on_new_page(self, page: Page):
        """拦截浏览器新建标签页的事件"""
        delayed_task = asyncio.create_task(self._delayed_check(page))
        self._track_gc_background_task(delayed_task)

    def _track_gc_background_task(self, task: asyncio.Task) -> None:
        """持有当前 CDP 会话的 GC 任务，并在任务结束后自动移除。"""
        self._gc_background_tasks.add(task)
        task.add_done_callback(self._gc_background_tasks.discard)

    async def _stop_gc_background_tasks(self) -> None:
        """停止旧 CDP 会话仍在运行的延迟识别和业务页 GC 任务。"""
        gc_tasks = list(self._gc_background_tasks)
        for task in gc_tasks:
            task.cancel()
        if gc_tasks:
            await asyncio.gather(*gc_tasks, return_exceptions=True)
        self._gc_background_tasks.clear()

    def _is_gc_managed_page_url(self, url: str) -> bool:
        """判断 URL 是否属于应由 Context 级 GC 管理的业务执行页面。"""
        normalized_url = url.lower()
        return any(
            marker.lower() in normalized_url
            for marker in self.gc_page_url_markers
        )

    def _is_slider_page_url(self, url: str) -> bool:
        """判断 URL 是否属于需要自动处理的独立滑块页面。"""
        SLIDER_PAGE_URL_MARKERS = ("mobile.yangkeduo.com",)
        normalized_url = url.lower()
        return any(
            marker.lower() in normalized_url
            for marker in SLIDER_PAGE_URL_MARKERS
        )

    async def _solve_slider_page(self, page: Page) -> bool:
        """处理独立滑块页，并根据滑块控件状态判断是否通过。"""
        return await solve_closed_shadow_slider(page)

    def _remaining_gc_pages(self, context: BrowserContext) -> List[Page]:
        """返回 Context 中尚未关闭、且符合 GC URL 规则的业务执行页面。"""
        return [
            page
            for page in context.pages
            if not page.is_closed() and self._is_gc_managed_page_url(page.url)
        ]

    async def _close_gc_page_after_exception(
        self,
        page: Page,
        url_suffix: str,
        stage: str,
    ) -> None:
        """GC 监控发生异常后，限时关闭已确认的业务执行页面。"""
        if page.is_closed():
            logger.info(
                f"[GC Daemon] 业务执行网页 {url_suffix} 在{stage}异常后已关闭。"
            )
            return

        logger.warning(
            f"[GC Daemon] 业务执行网页 {url_suffix} 在{stage}发生异常，"
            "执行强制关闭。"
        )
        try:
            await asyncio.wait_for(page.close(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning(
                f"[GC Daemon] 业务执行网页 {url_suffix} 在{stage}异常后，"
                "强制关闭超过 10 秒仍未完成。"
            )
        except Exception as close_error:
            logger.warning(
                f"[GC Daemon] 业务执行网页 {url_suffix} 在{stage}异常后关闭失败: "
                f"{type(close_error).__name__}: {close_error}"
            )
        else:
            logger.info(
                f"[GC Daemon] 业务执行网页 {url_suffix} 在{stage}异常后已关闭。"
            )

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

        await self._close_remaining_gc_pages(context, "程序最终收尾")

    async def _close_remaining_gc_pages(
        self,
        context: BrowserContext,
        reason: str,
    ) -> None:
        """扫描并关闭 GC 管理范围内的残留业务页。"""
        try:
            remaining_pages = self._remaining_gc_pages(context)
        except Exception as error:
            logger.warning(f"{reason}扫描残留业务页面失败: {error}")
            return

        if not remaining_pages:
            logger.info(f"{reason}未发现残留业务执行页面。")
            return

        logger.warning(
            f"{reason}发现 {len(remaining_pages)} 个残留业务执行页面，"
            "开始立即关闭。"
        )

        close_results = await asyncio.gather(
            *(page.close() for page in remaining_pages),
            return_exceptions=True,
        )
        failed_count = sum(
            isinstance(result, BaseException) for result in close_results
        )
        if failed_count:
            logger.warning(
                f"{reason}有 {failed_count} 个业务执行页面关闭失败；"
                "记录后继续后续流程。"
            )
        else:
            logger.info(f"{reason}的残留业务执行页面已全部关闭。")

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
                # 与业务执行页 GC 相同：没有目标元素时长期挂起，不做固定频率的 DOM 扫描。
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
        后台垃圾回收协程：基于事件倒计时监控特定业务执行页的心跳。
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
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if page.is_closed():
                    logger.debug(
                        f"[GC Daemon] 业务执行网页 {url_suffix} 在等待心跳期间已关闭。"
                    )
                    break
                logger.exception(
                    f"[GC Daemon] 业务执行网页 {url_suffix} 等待同步成功心跳时发生异常: "
                    f"{type(error).__name__}: {error}"
                )
                await self._close_gc_page_after_exception(
                    page,
                    url_suffix,
                    "等待同步成功心跳",
                )
                break

            if toast_handle is None:
                # attached 状态正常不会返回 None，仅作为接口返回值的防御性处理。
                continue

            try:
                # 只等待当前节点隐藏或移除；后续成功提示不会替换本次等待目标。
                await toast_handle.wait_for_element_state("hidden", timeout=30000)
            except PlaywrightTimeoutError:
                if not page.is_closed():
                    logger.warning(f"[GC Daemon] 业务执行网页 {url_suffix} 当前心跳弹窗节点超过 30 秒仍未隐藏，判定页面状态异常，执行强制关闭。")
                    try:
                        await page.close()
                    except Exception as e:
                        logger.warning(f"[GC Daemon] 关闭网页发生异常: {e}")
                break
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if page.is_closed():
                    logger.debug(
                        f"[GC Daemon] 业务执行网页 {url_suffix} 在等待心跳提示隐藏期间已关闭。"
                    )
                    break
                logger.exception(
                    f"[GC Daemon] 业务执行网页 {url_suffix} 等待心跳提示隐藏时发生异常: "
                    f"{type(error).__name__}: {error}"
                )
                await self._close_gc_page_after_exception(
                    page,
                    url_suffix,
                    "等待心跳提示隐藏",
                )
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
            # ElementUI 可能在可见性检查与实际点击之间完成退出动画并移除节点。
            # 此时普通点击会等待旧 Locator 至超时，但弹窗实际上已经关闭，
            # 不应继续等待 DOM 降级点击，更不能误判为 Worker 无响应。
            if not await self._locator_is_visible(
                container,
                worker_id,
                layer_name,
            ):
                logger.info(
                    f"Worker-{worker_id} {layer_name}已在点击等待期间自行关闭。"
                )
                return True

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

    async def _close_message_box_if_visible(
        self,
        page: Page,
        worker_id: str,
    ) -> bool:
        """关闭遮挡任务弹窗的最上层可见 ElementUI MessageBox。"""
        message_box = page.locator("div.el-message-box:visible").last
        if not await self._locator_is_visible(
            message_box,
            worker_id,
            "提示弹窗",
        ):
            return False

        close_button = message_box.locator("i.el-icon-close")
        close_count = await self._await_page_operation(
            close_button.count(),
            worker_id,
            "查询提示弹窗关闭按钮数量",
        )
        if close_count != 1:
            raise RuntimeError(
                f"Worker-{worker_id} 提示弹窗内部预期 1 个关闭按钮，"
                f"实际找到 {close_count} 个"
            )

        logger.info(f"Worker-{worker_id} 正在关闭页面提示弹窗...")
        await self._await_page_operation(
            close_button.evaluate("node => node.click()"),
            worker_id,
            "精准点击提示弹窗关闭按钮",
        )

        try:
            await message_box.wait_for(state="hidden", timeout=5000)
        except PlaywrightTimeoutError as error:
            await self._assert_page_healthy(page, worker_id)
            if not await self._locator_is_visible(
                message_box,
                worker_id,
                "提示弹窗",
            ):
                logger.info(f"Worker-{worker_id} 提示弹窗已在超时边界完成关闭。")
                return True
            raise WorkerUnresponsiveError(
                f"Worker-{worker_id} 提示弹窗关闭指令已发出，但弹窗仍未隐藏"
            ) from error

        logger.info(f"Worker-{worker_id} 页面提示弹窗已关闭。")
        return True

    async def _restore_primary_state(self, page: Page, worker_id: str):
        """依次关闭三级、二级弹窗，恢复到可操作的一级弹窗。"""
        await self._close_message_box_if_visible(page, worker_id)
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
            await primary_drawer.locator("#checkbutn").click(
                trial=True,
                timeout=self.primary_actionability_timeout_ms,
            )
        except PlaywrightTimeoutError as error:
            await self._assert_page_healthy(page, worker_id)
            raise WorkerUnresponsiveError(
                f"Worker-{worker_id} 恢复一级弹窗后【启动检测】按钮仍不可点击"
            ) from error

    async def _close_all_task_layers(self, page: Page, worker_id: str):
        """Worker 初始化时按层级关闭三级、二级和一级弹窗。"""
        await self._close_message_box_if_visible(page, worker_id)
        await self._close_layer_if_visible(
            self._progress_dialog(page), "三级进度弹窗", worker_id
        )
        await self._close_layer_if_visible(
            self._secondary_drawer(page), "二级补采弹窗", worker_id
        )
        await self._close_layer_if_visible(
            self._primary_drawer(page), "一级任务弹窗", worker_id
        )

    async def _open_task_card_by_id(
        self,
        page: Page,
        task_card_id: int,
    ) -> Optional[str]:
        """按数仓任务 ID 查询卡片，读取任务名称后打开唯一结果。"""
        id_input = page.locator("input.el-input__inner").nth(0)
        search_button = page.locator(
            "button.el-button.el-button--default"
        ).nth(5)
        result_card = page.locator(
            "div.workTool_page_card_test_dataCard"
        ).nth(0)
        expected_id_marker = result_card.locator(
            f'span.timeInfo[title="任务ID：{task_card_id}"]'
        )

        await id_input.fill(str(task_card_id))
        await search_button.click(timeout=30000)
        await expected_id_marker.wait_for(state="visible", timeout=45000)

        task_name: Optional[str] = None
        title_element = result_card.locator(
            "span.workTool_page_card_test_dataCard_title_span"
        )
        try:
            raw_task_name = await title_element.evaluate(
                """
                element => Array.from(element.childNodes)
                    .filter(node => node.nodeType === 3)
                    .map(node => (node.textContent || "").trim())
                    .filter(Boolean)
                    .join(" ")
                """
            )
            normalized_task_name = str(raw_task_name).strip()
            if normalized_task_name:
                task_name = normalized_task_name
            else:
                logger.warning(
                    f"任务 ID {task_card_id} 的卡片标题为空，task_name 将记为 null。"
                )
        except Exception as error:
            if self._fatal_page_error_reason(error):
                raise
            logger.warning(
                f"读取任务 ID {task_card_id} 的任务名称失败，"
                f"task_name 将记为 null: {error}"
            )

        await result_card.click(timeout=30000)
        return task_name

    async def inject_dates(self, page: Page, start_date: str, end_date: str, worker_id: str):
        """
        日期注入逻辑：通过模拟真实的物理键盘事件，确保触发 Vue 框架的数据双向绑定。
        """
        logger.info(f"Worker-{worker_id} 开始注入采集区间: {start_date} 至 {end_date}")
        try:
            primary_drawer = self._primary_drawer(page)
            start_input = primary_drawer.get_by_role(
                "textbox", name="开始", exact=False
            )
            end_input = primary_drawer.get_by_role(
                "textbox", name="结束", exact=False
            )

            start_input_count = await self._await_page_operation(
                start_input.count(),
                worker_id,
                "查询可见开始日期输入框数量",
            )
            end_input_count = await self._await_page_operation(
                end_input.count(),
                worker_id,
                "查询可见结束日期输入框数量",
            )
            if start_input_count != 1 or end_input_count != 1:
                raise RuntimeError(
                    "一级弹窗内预期各找到 1 个可见的开始/结束日期输入框，"
                    f"实际找到 {start_input_count}/{end_input_count} 个"
                )
            
            # 填充开始日期并按回车确认
            await start_input.fill(start_date)
            await start_input.press("Enter")
            await page.wait_for_timeout(200) # 给 UI 一点反应时间
            
            # 填充结束日期并按回车确认
            await end_input.fill(end_date)
            await end_input.press("Enter")
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
    ) -> Optional[int]:
        """重新请求后端检测缺失量；0=无缺失，正整数=缺失量，None=不确定。"""
        start_btn = primary_drawer.locator("#checkbutn")
        result_title = primary_drawer.locator(
            "div.testContent_list_title_dayType"
        ).first
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
                    await result_title.wait_for(state="visible", timeout=45000)
                except PlaywrightTimeoutError:
                    logger.warning(
                        f"Worker-{worker_id} {phase}等待查询结果标题渲染超时，"
                        "继续读取缺失统计。"
                    )

                # 结果项标题可见后，再给顶部缺失统计文本一个短暂渲染缓冲。
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
                return 0

            missing_count = int(match.group())
            if missing_count > 0:
                log_method = logger.warning if phase == "终态复检" else logger.info
                log_method(
                    f"Worker-{worker_id} {phase}统计文本 [{missing_text}]，"
                    f"确认仍有 {missing_count} 条缺失数据。"
                )
                return missing_count

            logger.warning(
                f"Worker-{worker_id} {phase}统计文本 [{missing_text}] 显示 0 或负数，"
                "视为前端渲染异常并重新检测。"
            )

        logger.error(
            f"Worker-{worker_id} {phase}连续 3 次仍无法获得可信缺失量。"
        )
        return None

    async def _read_auto_detection_result(
        self,
        page: Page,
        worker_id: str,
    ) -> Optional[int]:
        """读取自动检测结果；0=无缺失，正整数=缺失量，None=不可信。"""
        # 完成弹窗会立即触发自动检测；短暂缓冲用于避开上一轮结果尚未清空的瞬间。
        await page.wait_for_timeout(2000)

        primary_drawer = self._primary_drawer(page)
        result_title = primary_drawer.locator(
            "div.testContent_list_title_dayType"
        ).first
        missing_span = primary_drawer.locator(
            "div.testContent > div:nth-child(2) > span:nth-child(1)"
        )

        await result_title.wait_for(state="visible", timeout=45000)

        for read_attempt, retry_delay_ms in enumerate(
            (0, 2000, 4000), start=1
        ):
            if retry_delay_ms:
                logger.info(
                    f"Worker-{worker_id} 自动检测统计文本仍在渲染，"
                    f"等待 {retry_delay_ms // 1000} 秒后进行第 "
                    f"{read_attempt}/3 次读取。"
                )
                await page.wait_for_timeout(retry_delay_ms)

            await missing_span.wait_for(state="attached", timeout=30000)
            missing_text = (await missing_span.inner_text()).strip()
            if missing_text != "：表示缺失数据":
                break
        else:
            raise MissingDataRenderError(
                f"Worker-{worker_id} 捕获数据补齐完成信号后，自动检测统计文本"
                "连续 3 次仍为渲染占位符。"
            )

        match = re.search(r"-?\d+", missing_text)
        if not match:
            logger.info(
                f"Worker-{worker_id} 自动检测统计文本 [{missing_text}] 不含数字，"
                "确认当前日期无缺失数据。"
            )
            return 0

        missing_count = int(match.group())
        if missing_count > 0:
            logger.warning(
                f"Worker-{worker_id} 自动检测统计文本 [{missing_text}]，"
                f"确认仍有 {missing_count} 条缺失数据。"
            )
            return missing_count

        logger.warning(
            f"Worker-{worker_id} 自动检测统计文本 [{missing_text}] 显示 0 或负数，"
            "无法作为可信的成功依据。"
        )
        return None

    async def _read_detail_missing_categories(self, page: Page) -> List[str]:
        """读取当前一级弹窗中所有 loseItem 类目的文本，保持页面顺序。"""
        primary_drawer = self._primary_drawer(page)
        missing_items = primary_drawer.locator(
            "div.testContent_list_title_testLine_item.loseItem"
        )
        categories: List[str] = []
        for index in range(await missing_items.count()):
            category = (
                await missing_items.nth(index).locator("span").first.inner_text()
            ).strip()
            if category:
                categories.append(category)
        return categories

    async def _finish_after_completion_signal(
        self,
        page: Page,
        worker_id: str,
    ) -> Optional[int]:
        """完成信号只表示队列遍历结束，最终结果以自动检测缺失量为准。"""
        logger.info(
            f"Worker-{worker_id} 捕获到数据补齐完成信号，"
            "等待网页自动检测完成。"
        )
        missing_count = await self._read_auto_detection_result(
            page,
            worker_id,
        )
        if missing_count == 0:
            logger.info(
                f"Worker-{worker_id} 自动检测确认无缺失数据，当前任务成功。"
            )
            return 0
        if missing_count is not None:
            logger.warning(
                f"Worker-{worker_id} 队列已遍历完成，但自动检测仍有 "
                f"{missing_count} 条缺失数据，"
                "当前任务失败并交由调度器决定是否重试。"
            )
            return missing_count

        logger.warning(
            f"Worker-{worker_id} 队列已遍历完成，但自动检测结果不可信，"
            "当前任务不写入成功结果。"
        )
        return None

    @staticmethod
    async def _cancel_wait_task(task: asyncio.Task) -> None:
        """取消竞争等待中已不再需要的任务，并回收其异常。"""
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # 另一个竞争信号已经决定流程，不让被取消分支覆盖主结果。
            pass

    async def _wait_for_next_heartbeat(
        self,
        page: Page,
        worker_id: str,
    ) -> bool:
        """完整跟踪一个同步心跳；False表示应进入静默兜底。"""
        toast_handle: Optional[ElementHandle] = None
        try:
            try:
                toast_handle = await page.wait_for_selector(
                    ".el-message__content:has-text('同步成功')",
                    state="attached",
                    timeout=self.silent_timeout_seconds * 1000,
                )
            except PlaywrightTimeoutError:
                logger.info(
                    f"Worker-{worker_id} 超过 {self.silent_timeout_seconds} "
                    "秒无新信号，开始终态复检。"
                )
                return False

            if toast_handle is None:
                return True

            try:
                await toast_handle.wait_for_element_state(
                    "hidden",
                    timeout=30000,
                )
            except PlaywrightTimeoutError:
                logger.warning(
                    f"Worker-{worker_id} 当前心跳弹窗节点超过 30 秒仍未隐藏，"
                    "停止心跳监听并进入终态检查。"
                )
                return False
            return True
        finally:
            if toast_handle is not None:
                try:
                    await toast_handle.dispose()
                except Exception:
                    # 页面关闭或崩溃时，释放句柄本身也可能失败。
                    pass

    async def wait_for_completion_or_heartbeat(
        self,
        page: Page,
        worker_id: str,
        start_date: str,
        end_date: str,
    ) -> Optional[int]:
        """
        完成弹窗触发自动检测结果核验；普通心跳维持监听，静默时保留后端复检兜底。
        """
        completion_selector = ".el-message__content:has-text('数据补齐完成')"
        completion_handle: Optional[ElementHandle] = None

        logger.info(
            f"Worker-{worker_id} 开始监听同步心跳与数据补齐完成信号（超过 "
            f"{self.silent_timeout_seconds} 秒无新信号则进入后端终态复检）..."
        )

        # 完成监听贯穿整个心跳循环，避免它在普通心跳隐藏期间出现而被漏掉。
        completion_task = asyncio.create_task(
            page.wait_for_selector(
                completion_selector,
                state="attached",
                timeout=0,
            )
        )

        try:
            while True:
                heartbeat_task = asyncio.create_task(
                    self._wait_for_next_heartbeat(page, worker_id)
                )
                done, _ = await asyncio.wait(
                    {completion_task, heartbeat_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # 两种提示几乎同时出现时，完成信号拥有更高优先级。
                if completion_task in done:
                    await self._cancel_wait_task(heartbeat_task)
                    completion_handle = completion_task.result()
                    return await self._finish_after_completion_signal(
                        page,
                        worker_id,
                    )

                if not heartbeat_task.result():
                    # 给恰好处于超时边界的完成事件一次调度机会。
                    await asyncio.sleep(0)
                    if completion_task.done():
                        completion_handle = completion_task.result()
                        return await self._finish_after_completion_signal(
                            page,
                            worker_id,
                        )
                    break
        except Exception as error:
            logger.error(f"Worker-{worker_id} 监听过程中发生异常: {error}")
            raise
        finally:
            if not completion_task.done():
                await self._cancel_wait_task(completion_task)
            elif completion_handle is None:
                # 静默超时边界上完成信号可能刚好到达；流程虽走兜底，句柄仍需释放。
                unused_handle = completion_task.result()
                if unused_handle is not None:
                    try:
                        await unused_handle.dispose()
                    except Exception:
                        pass
            if completion_handle is not None:
                try:
                    await completion_handle.dispose()
                except Exception:
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

        if terminal_missing == 0:
            logger.info(
                f"Worker-{worker_id} 终态复检确认无缺失数据，当前任务成功。"
            )
            return 0
        if terminal_missing is not None:
            logger.warning(
                f"Worker-{worker_id} 终态复检仍有 {terminal_missing} 条缺失数据，"
                "当前任务失败。"
            )
            return terminal_missing

        logger.warning(
            f"Worker-{worker_id} 终态复检结果不确定，不写入成功结果。"
        )
        return None

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
                        "missing_count": None,
                        "detail_missing_categories": None,
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
        task_card_id = task["card"]
        # 每次 attempt 都必须重新产生终态结果，不能继承上一次失败详情。
        task["missing_count"] = None
        task["detail_missing_categories"] = None
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
                
            # 2. 按任务 ID 查询并进入唯一匹配的任务卡片。
            task_name = await self._open_task_card_by_id(
                page,
                task_card_id,
            )
            if task_name:
                task["task_name"] = task_name
            
            # 等待包含启动按钮的一级弹窗真正展开。
            primary_drawer = self._primary_drawer(page)
            await primary_drawer.wait_for(
                state="visible",
                timeout=30000,
            )
            await primary_drawer.locator("#checkbutn").click(
                trial=True,
                timeout=self.primary_actionability_timeout_ms,
            )
            logger.info(f"Worker-{worker_id} 初始化完成，已成功进入补采专属弹窗！")
            
            # 不需要记录基准线，依靠锚点即可
        except PageProbeTimeoutError as e:
            logger.warning(
                f"Worker-{worker_id} 初始化期间页面探针超时，"
                f"等待 {self.page_probe_cooldown_seconds} 秒后将当前任务记为失败；"
                f"Worker保持运行: {e}"
            )
            await asyncio.sleep(self.page_probe_cooldown_seconds)
            return False
        except Exception as e:
            fatal_reason = self._fatal_page_error_reason(e)
            if fatal_reason:
                logger.error(f"Worker-{worker_id} 初始化期间检测到致命页面异常（{fatal_reason}），停止该Worker: {e}")
                raise
            logger.error(f"Worker-{worker_id} 任务页面初始化失败: {e}")
            await asyncio.sleep(self.initialization_failure_cooldown_seconds)
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

                if detection_result == 0:
                    task["missing_count"] = 0
                    task["detail_missing_categories"] = []
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
                
                # 4. 完成弹窗触发自动检测核验；若未捕获，则在心跳静默后执行后端复检。
                missing_count = await self.wait_for_completion_or_heartbeat(
                    page,
                    worker_id,
                    start_date,
                    end_date,
                )
                task["missing_count"] = missing_count
                if missing_count is None:
                    task["detail_missing_categories"] = None
                elif missing_count == 0:
                    task["detail_missing_categories"] = []
                else:
                    try:
                        task["detail_missing_categories"] = (
                            await self._read_detail_missing_categories(page)
                        )
                    except Exception as detail_error:
                        task["detail_missing_categories"] = None
                        logger.warning(
                            f"Worker-{worker_id} 读取终态缺失类目失败，"
                            f"不影响 missing_count 判定: {detail_error}"
                        )
                completed_normally = missing_count == 0
                if completed_normally:
                    logger.info(f"Worker-{worker_id} 成功跑完任务: {start_date} 至 {end_date}")
                else:
                    logger.warning(
                        f"Worker-{worker_id} 当前区间终态复检未通过: "
                        f"{start_date} 至 {end_date}"
                    )
                return completed_normally
                
            except PageProbeTimeoutError as e:
                current_state = (
                    "已经提交【全店补齐】，但最终结果未知"
                    if task_submitted
                    else "尚未完成【全店补齐】提交"
                )
                logger.warning(
                    f"Worker-{worker_id} 在执行 {start_date} 至 {end_date} "
                    f"期间页面探针超时（{current_state}），等待 "
                    f"{self.page_probe_cooldown_seconds} 秒后将当前任务记为失败；"
                    f"Worker保持运行: {e}"
                )
                await asyncio.sleep(self.page_probe_cooldown_seconds)
                return False
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
        session_control: CdpSessionControl,
    ) -> bool:
        """在单次 CDP 生命周期内持续消费顶层共享队列。"""
        worker_id = f"页面-{list_index + 1}"
        logger.info(
            f"Worker-{worker_id} 启动持续任务池，绑定页面: {page.url[-25:]}"
        )
        consecutive_initialization_failures = 0

        while True:
            task = await self._get_task_for_session(
                task_queue,
                session_control,
            )
            if task is None:
                logger.info(f"Worker-{worker_id} 已停止领取新任务。")
                return True

            worker_fatal = False
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
                    worker_fatal = True
                    logger.error(
                        f"Worker-{worker_id} 已达到连续初始化失败阈值，"
                        "触发熔断并停止领取新任务。"
                    )
            except Exception as error:
                success = False
                if self._is_driver_connection_error(error):
                    worker_fatal = True
                    session_control.request_stop("connection_lost")
                    logger.error(
                        f"Worker-{worker_id} 检测到 Playwright Driver/CDP "
                        f"连接失效，停止当前会话: {error}"
                    )
                else:
                    fatal_reason = self._fatal_page_error_reason(error)
                    if fatal_reason:
                        # 单个 Page 被关闭、崩溃或失去响应，只隔离当前 Worker。
                        worker_fatal = True
                        logger.error(
                            f"Worker-{worker_id} 因{fatal_reason}停止领取新任务。"
                        )
                    else:
                        logger.error(
                            f"Worker-{worker_id} 执行任务时发生未分类异常，"
                            f"当前任务记为失败: {error}"
                        )

            try:
                await ledger.record(task, success)
                if not success and task["attempt"] < self.max_attempts:
                    retry_task = {
                        **task,
                        "attempt": task["attempt"] + 1,
                        "missing_count": None,
                        "detail_missing_categories": None,
                    }
                    task_queue.put_nowait(retry_task)
                    logger.warning(
                        f"任务 {task['task_id']} 第 {task['attempt']}/"
                        f"{self.max_attempts} 次执行失败，已放回共享队列尾部。"
                    )
                elif not success:
                    logger.error(
                        f"任务 {task['task_id']} 已达到最大执行次数 "
                        f"{self.max_attempts}，最终记为失败。"
                    )
            except Exception as error:
                session_control.request_stop("ledger_error")
                worker_fatal = True
                logger.exception(
                    f"任务 {task['task_id']} 写入账本失败，停止当前会话: {error}"
                )
            finally:
                # 重试任务必须先入队再结束当前项，避免 queue.join() 提前返回。
                task_queue.task_done()

            if worker_fatal:
                return False

    async def _get_task_for_session(
        self,
        task_queue: asyncio.Queue,
        session_control: CdpSessionControl,
    ) -> Optional[Dict[str, Any]]:
        """等待队列任务；截止或停止信号发生后不再领取。"""
        if session_control.stop_event.is_set():
            return None

        remaining_seconds = session_control.deadline - time.monotonic()
        if remaining_seconds <= 0:
            session_control.request_stop("lifetime_expired")
            return None

        get_task = asyncio.create_task(task_queue.get())
        stop_task = asyncio.create_task(session_control.stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {get_task, stop_task},
                timeout=remaining_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if get_task not in done:
                if not done:
                    session_control.request_stop("lifetime_expired")
                # stop/deadline 先赢时，get 仍可能在 cancel 前恰好完成。
                get_task.cancel()
                await asyncio.gather(get_task, return_exceptions=True)
                if (
                    not get_task.cancelled()
                    and get_task.exception() is None
                ):
                    unstarted_task = get_task.result()
                    task_queue.put_nowait(unstarted_task)
                    task_queue.task_done()
                return None

            task = get_task.result()
            # queue.get 与停止信号可能同时完成。任务尚未开始时必须原样归还。
            if (
                stop_task in done
                or session_control.stop_event.is_set()
                or time.monotonic() >= session_control.deadline
            ):
                if time.monotonic() >= session_control.deadline:
                    session_control.request_stop("lifetime_expired")
                task_queue.put_nowait(task)
                task_queue.task_done()
                return None
            return task
        finally:
            for wait_task in (get_task, stop_task):
                if not wait_task.done():
                    wait_task.cancel()
            await asyncio.gather(get_task, stop_task, return_exceptions=True)

    async def _run_task_pool_session(
        self,
        worker_pages: List[Page],
        task_queue: asyncio.Queue,
        ledger: TaskLedger,
        session_control: CdpSessionControl,
    ) -> CdpSessionResult:
        """运行一个 CDP 生命周期；队列由 Backfill 顶层持有。"""
        if not worker_pages:
            session_control.request_stop("no_workers")
            logger.error("当前 CDP 会话未找到可用 Worker；队列保持原状。")
            return CdpSessionResult("no_workers", True, 0)

        logger.info(
            f"CDP 会话任务池开始：队列当前 {task_queue.qsize()} 项，"
            f"{len(worker_pages)} 个 Worker，每个任务最多 {self.max_attempts} 次。"
        )
        worker_tasks = [
            asyncio.create_task(
                self.worker(
                    page,
                    task_queue,
                    ledger,
                    index,
                    session_control,
                )
            )
            for index, page in enumerate(worker_pages)
        ]
        workers_done = asyncio.gather(*worker_tasks, return_exceptions=True)
        queue_done = asyncio.create_task(task_queue.join())
        stop_wait = asyncio.create_task(session_control.stop_event.wait())

        async def expire_session() -> None:
            remaining = session_control.deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            session_control.request_stop("lifetime_expired")

        lifetime_task = asyncio.create_task(expire_session())
        queue_completed = False
        try:
            done, _ = await asyncio.wait(
                {workers_done, queue_done, stop_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if queue_done in done:
                session_control.request_stop("tasks_completed")

            worker_results = await workers_done
            # task_done() 唤醒 join() 需要一次事件循环调度机会。
            await asyncio.sleep(0)
            queue_completed = (
                queue_done.done()
                and not queue_done.cancelled()
                and queue_done.exception() is None
            )
            all_workers_failed = all(
                result is False or isinstance(result, BaseException)
                for result in worker_results
            )
            if not queue_done.done() and (
                all_workers_failed or session_control.end_reason is None
            ):
                session_control.request_stop("no_workers")
            for index, result in enumerate(worker_results):
                if isinstance(result, BaseException):
                    session_control.request_stop("session_error")
                    logger.error(
                        f"Worker-页面-{index + 1} 协程异常退出: {result}"
                    )
        finally:
            for wait_task in (queue_done, stop_wait, lifetime_task):
                if not wait_task.done():
                    wait_task.cancel()
            await asyncio.gather(
                queue_done,
                stop_wait,
                lifetime_task,
                return_exceptions=True,
            )
            for worker_task in worker_tasks:
                if not worker_task.done():
                    worker_task.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)

        reason = session_control.end_reason or "tasks_completed"
        logger.info(
            f"CDP 会话任务池结束：reason={reason}，"
            f"队列保留 {task_queue.qsize()} 项。"
        )
        return CdpSessionResult(
            reason=reason,
            ledger_ready=reason != "ledger_error",
            worker_count=len(worker_pages),
            queue_completed=queue_completed,
        )

    async def _run_cdp_session(
        self,
        browser: Browser,
        task_queue: asyncio.Queue,
        ledger: TaskLedger,
    ) -> CdpSessionResult:
        """校验 BrowserContext/Worker，并完整收束一次会话的后台协程。"""
        try:
            contexts = browser.contexts
            if not contexts:
                logger.error(
                    "浏览器中没有默认 Context；不能创建隔离 Context 代替登录态。"
                )
                return CdpSessionResult("no_workers", True, 0)

            context = contexts[0]
            context.on("page", self._on_new_page)
            for existing_page in context.pages:
                self._on_new_page(existing_page)

            worker_pages = [
                page for page in context.pages if "datatoolcenter" in page.url
            ]
            if not worker_pages:
                logger.error(
                    "当前 CDP 会话未找到 datatoolcenter Worker 页面，停止运行。"
                )
                return CdpSessionResult("no_workers", True, 0)

            logger.info(f"检测到 {len(worker_pages)} 个符合条件的 Worker 标签页。")
            session_control = CdpSessionControl(
                deadline=time.monotonic() + self.cdp_session_lifetime_seconds
            )

            def on_disconnected(_browser: Browser) -> None:
                logger.error("检测到 Browser disconnected，停止当前 CDP 会话领取。")
                session_control.request_stop("connection_lost")

            browser.on("disconnected", on_disconnected)
            error_toast_monitors = [
                asyncio.create_task(
                    self._monitor_worker_error_toasts(
                        page,
                        f"页面-{index + 1}",
                    )
                )
                for index, page in enumerate(worker_pages)
            ]
            try:
                result = await self._run_task_pool_session(
                    worker_pages,
                    task_queue,
                    ledger,
                    session_control,
                )
                if (
                    result.reason == "tasks_completed"
                    or (
                        result.queue_completed
                        and browser.is_connected()
                    )
                ):
                    await self._cleanup_remaining_gc_pages(context)
                final_reason = session_control.end_reason or result.reason
                if final_reason != result.reason:
                    result = CdpSessionResult(
                        reason=final_reason,
                        ledger_ready=result.ledger_ready,
                        worker_count=result.worker_count,
                        queue_completed=result.queue_completed,
                    )
                return result
            finally:
                try:
                    browser.remove_listener("disconnected", on_disconnected)
                except Exception:
                    pass
                await self._stop_error_toast_monitors(error_toast_monitors)
                await self._stop_gc_background_tasks()
        except Exception as error:
            reason = (
                "connection_lost"
                if self._is_driver_connection_error(error)
                else "session_error"
            )
            logger.exception(f"CDP 会话主流程异常（{reason}）: {error}")
            await self._stop_gc_background_tasks()
            return CdpSessionResult(reason, reason != "ledger_error", -1)

    @staticmethod
    def _log_summary(summary: Dict[str, int]) -> None:
        """输出逐次 attempt 和最终完成情况。"""
        lines = [
            "\n任务执行汇总：",
            f"  配置任务总数：{summary['total']}",
        ]
        for attempt_result in summary.get("attempt_stats", []):
            lines.append(
                f"  第 {attempt_result['attempt']} 次尝试："
                f"{attempt_result['success']} 成功 / "
                f"{attempt_result['failed']} 失败 / "
                f"{attempt_result['total']} 条记录"
            )
        lines.extend(
            [
                f"  最终完成：{summary['final_success']}",
                f"  最终失败（含未领取任务）：{summary['final_failed']}",
            ]
        )
        logger.info("\n".join(lines))

    async def _browser_identity_matches(
        self,
        cdp_address: str,
        expected_identity: str,
    ) -> bool:
        """只读确认缓存 CDP 端点仍属于初次连接的同一个浏览器。"""
        current_identity = await asyncio.to_thread(
            get_cdp_browser_identity,
            cdp_address,
        )
        if current_identity is None:
            logger.error("缓存 CDP 端点已不可达，浏览器可能已关闭。")
            return False
        if current_identity != expected_identity:
            logger.error("缓存 CDP 端点的浏览器身份已变化，拒绝接管替代进程。")
            return False
        return True

    async def run(self, tasks_config: list = None):
        logger.info(
            "历史补采启动："
            f"浏览器连接器={type(self.browser_connector).__name__}，"
            f"Worker心跳静默阈值={self.silent_timeout_seconds}秒，"
            f"业务页心跳静默阈值={self.gc_silent_timeout_seconds}秒，"
            f"任务最多尝试={self.max_attempts}次，"
            f"CDP会话软生命周期={self.cdp_session_lifetime_hours:g}小时，"
            f"最多重建={self.max_cdp_rebuilds}次"
        )
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

        task_queue: asyncio.Queue = asyncio.Queue()
        for task in initial_tasks:
            task_queue.put_nowait(task)
        logger.info(
            f"本轮任务账本已重置: {ledger.path}；日志继续追加到: {log_path}；"
            f"持久化内存队列已装入 {len(initial_tasks)} 个任务。"
        )

        # 仅初次启动调用连接器；后续生命周期禁止再次触发 /browser/open。
        cdp_address = self.browser_connector.get_cdp_address()
        if not cdp_address:
            logger.error("无法获取浏览器 CDP 地址，程序退出。")
            self._log_summary(await ledger.summary(len(initial_tasks)))
            return
        cdp_address = normalize_cdp_address(cdp_address)
        initial_identity = await asyncio.to_thread(
            get_cdp_browser_identity,
            cdp_address,
        )
        if not initial_identity:
            logger.error("无法读取初始 CDP 浏览器身份，程序退出。")
            self._log_summary(await ledger.summary(len(initial_tasks)))
            return

        rebuild_count = 0
        first_session = True
        while True:
            if not first_session:
                if rebuild_count >= self.max_cdp_rebuilds:
                    logger.error(
                        f"CDP 会话已达到最大重建次数 {self.max_cdp_rebuilds}，"
                        f"队列仍保留 {task_queue.qsize()} 个未完成项。"
                    )
                    break
                if not await self._browser_identity_matches(
                    cdp_address,
                    initial_identity,
                ):
                    logger.error(
                        f"浏览器已关闭或被替换；队列保留 "
                        f"{task_queue.qsize()} 个未完成项，Backfill 彻底停止。"
                    )
                    break
                rebuild_count += 1
                logger.warning(
                    f"开始第 {rebuild_count}/{self.max_cdp_rebuilds} 次 CDP 重建；"
                    "只重建 Playwright Driver/代理，不关闭或重启浏览器。"
                )

            first_session = False
            connected = False
            try:
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.connect_over_cdp(
                        f"http://{cdp_address}"
                    )
                    connected = True
                    if rebuild_count > 0:
                        if browser.contexts:
                            await self._close_remaining_gc_pages(
                                browser.contexts[0],
                                "CDP 重建预清理",
                            )
                        logger.info(
                            f"CDP 重建连接完成：第 {rebuild_count}/"
                            f"{self.max_cdp_rebuilds} 次；"
                            "已重新连接原浏览器，"
                            f"队列剩余 {task_queue.qsize()} 项，"
                            "开始校验 Worker 并恢复任务调度。"
                        )
                    session_result = await self._run_cdp_session(
                        browser,
                        task_queue,
                        ledger,
                    )
            except Exception as error:
                reason = (
                    "connection_lost"
                    if not connected or self._is_driver_connection_error(error)
                    else "session_error"
                )
                logger.exception(f"建立或退出 CDP 会话失败（{reason}）: {error}")
                session_result = CdpSessionResult(reason, True, -1)
            finally:
                # 防止连接阶段异常时遗留上一会话的后台任务引用。
                await self._stop_gc_background_tasks()

            if session_result.reason in {"ledger_error", "session_error"}:
                logger.error(
                    f"发生不可安全恢复的内部错误 {session_result.reason}，"
                    f"队列保留 {task_queue.qsize()} 个未完成项。"
                )
                break
            if (
                session_result.reason == "tasks_completed"
                or session_result.queue_completed
            ):
                logger.info("持久化任务队列已完成。")
                break
            if session_result.worker_count == 0:
                logger.error(
                    f"当前连接没有默认 Context 或 Worker 页面，"
                    f"队列保留 {task_queue.qsize()} 个未完成项，停止运行。"
                )
                break

        summary = await ledger.summary(total_tasks=len(initial_tasks))
        self._log_summary(summary)


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


@dataclass(frozen=True)
class BackfillRuntimeConfig:
    """历史补采从 .env 解析出的浏览器、任务和心跳配置。"""

    browser_type: str
    bite_id: Optional[str]
    cdp_address: Optional[str]
    tasks_config: List[Dict[str, Any]]
    gc_page_url_markers: List[str]
    worker_heartbeat_silence_seconds: int
    business_heartbeat_silence_seconds: int
    max_attempts: int
    cdp_session_lifetime_hours: float
    max_cdp_rebuilds: int


def _load_positive_int_env(name: str, default: int) -> int:
    raw_value = (os.getenv(name) or "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f".env 中的 {name} 必须是整数") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f".env 中的 {name} 必须大于 0")
    return value


def _load_nonnegative_int_env(name: str, default: int) -> int:
    raw_value = (os.getenv(name) or "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f".env 中的 {name} 必须是整数") from error
    if value < 0:
        raise ValueError(f".env 中的 {name} 不能小于 0")
    return value


def _load_positive_float_env(name: str, default: float) -> float:
    raw_value = (os.getenv(name) or "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as error:
        raise ValueError(f".env 中的 {name} 必须是数字") from error
    if value <= 0:
        raise ValueError(f".env 中的 {name} 必须大于 0")
    return value


def load_runtime_config() -> BackfillRuntimeConfig:
    """从源码或 exe 同目录的 .env 加载本次运行配置。"""
    env_path = runtime_dir / ".env"
    if not env_path.exists():
        raise FileNotFoundError(
            f"未找到运行配置 {env_path}；请复制 backfill.env.example 为 .env 后填写。"
        )

    load_dotenv(env_path)
    browser_type = (os.getenv("BROWSER_TYPE") or "bitbrowser").strip().lower()
    if browser_type not in {"bitbrowser", "external_cdp"}:
        raise ValueError(
            ".env 中的 BROWSER_TYPE 只能是 bitbrowser 或 external_cdp"
        )

    bite_id = (os.getenv("BITE_ID") or "").strip() or None
    cdp_address = (os.getenv("CDP_ADDRESS") or "").strip() or None
    if browser_type == "bitbrowser" and not bite_id:
        raise ValueError("BROWSER_TYPE=bitbrowser 时必须配置 BITE_ID")
    if browser_type == "external_cdp":
        if not cdp_address:
            # 兼容周末 Edge 试验版本；新配置统一使用 CDP_ADDRESS。
            legacy_address = (os.getenv("EDGE_CDP_ADDRESS") or "").strip()
            if legacy_address:
                cdp_address = legacy_address
                logger.warning(
                    "EDGE_CDP_ADDRESS 已兼容读取，后续请改用 CDP_ADDRESS"
                )
            else:
                raise ValueError(
                    "BROWSER_TYPE=external_cdp 时必须配置 CDP_ADDRESS"
                )
        cdp_address = normalize_cdp_address(cdp_address)

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

    worker_silence = _load_positive_int_env(
        "WORKER_HEARTBEAT_SILENCE_SECONDS",
        120,
    )
    business_silence = _load_positive_int_env(
        "BUSINESS_HEARTBEAT_SILENCE_SECONDS",
        180,
    )
    if business_silence <= worker_silence:
        raise ValueError(
            "BUSINESS_HEARTBEAT_SILENCE_SECONDS 必须大于 "
            "WORKER_HEARTBEAT_SILENCE_SECONDS"
        )

    return BackfillRuntimeConfig(
        browser_type=browser_type,
        bite_id=bite_id,
        cdp_address=cdp_address,
        tasks_config=tasks_config_raw,
        gc_page_url_markers=markers_raw,
        worker_heartbeat_silence_seconds=worker_silence,
        business_heartbeat_silence_seconds=business_silence,
        max_attempts=_load_positive_int_env("MAX_ATTEMPTS", 5),
        cdp_session_lifetime_hours=_load_positive_float_env(
            "BACKFILL_CDP_SESSION_LIFETIME_HOURS",
            3.5,
        ),
        max_cdp_rebuilds=_load_nonnegative_int_env(
            "BACKFILL_MAX_CDP_REBUILDS",
            5,
        ),
    )


if __name__ == "__main__":
    logger.info(
        "程序版本: app=backfill_engine, git_sha=%s",
        GIT_SHA,
    )
    try:
        config = load_runtime_config()
    except (OSError, ValueError) as error:
        logger.error(f"运行配置加载失败: {error}")
        sys.exit(1)

    if config.browser_type == "bitbrowser":
        browser_connector: BrowserConnector = BitBrowserConnector(
            config.bite_id or ""
        )
    else:
        browser_connector = ExternalCdpConnector(config.cdp_address or "")

    engine = BackfillEngine(
        config.bite_id,
        gc_page_url_markers=config.gc_page_url_markers,
        browser_connector=browser_connector,
        worker_heartbeat_silence_seconds=(
            config.worker_heartbeat_silence_seconds
        ),
        business_heartbeat_silence_seconds=(
            config.business_heartbeat_silence_seconds
        ),
        max_attempts=config.max_attempts,
        cdp_session_lifetime_hours=config.cdp_session_lifetime_hours,
        max_cdp_rebuilds=config.max_cdp_rebuilds,
    )
    asyncio.run(engine.run(config.tasks_config))
