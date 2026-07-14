#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RPA 每日任务调度入口：登录态重建、固定 Worker 和单任务即时重试。"""

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# 必须在导入 backfill_engine 前指定，避免 daily 日志写入历史补采日志。
os.environ.setdefault("RPA_LOG_FILENAME", "daily_run.log")
# daily-mode 默认只写日志文件；即使源码从终端运行也不输出控制台日志。
os.environ.setdefault("RPA_CONSOLE_LOGGING", "0")

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, async_playwright

from auth_manager import AuthReport, CookieAuthManager
from backfill_engine import (
    BackfillEngine,
    TaskPageInitializationError,
    logger,
    log_path,
    runtime_dir,
)
from browser_manager import BitBrowserManager
from task_ledger import TaskLedger


DEFAULT_TASK_URL = (
    "https://datatoolcenter.com/web/dateCenter.html?"
    "activeName=selfitemkeyShop&menuplat=%E5%B7%A5%E4%BD%9C%E5%8F%B0"
)


@dataclass(frozen=True)
class DailyRuntimeConfig:
    """从 EXE 同目录 .env 解析出的 daily-mode 运行配置。"""

    bite_id: str
    worker_count: int
    max_attempts: int
    target_date_offset_days: int
    target_date: Optional[str]
    cookie_dir: Path
    task_url: str
    daily_tasks: List[Dict[str, Any]]
    platforms: List[Dict[str, Any]]
    gc_page_url_markers: List[str]
    keep_browser_after_run: bool


def _require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ValueError(f".env 缺少必填配置 {name}")
    return value


def _load_json_list_env(name: str) -> List[Any]:
    raw_value = _require_env(name)
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ValueError(f".env 中的 {name} 不是有效 JSON 数组: {error}") from error
    if not isinstance(value, list):
        raise ValueError(f".env 中的 {name} 必须是 JSON 数组")
    return value


def _load_int_env(name: str, minimum: int) -> int:
    raw_value = _require_env(name)
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f".env 中的 {name} 必须是整数") from error
    if value < minimum:
        raise ValueError(f".env 中的 {name} 不能小于 {minimum}")
    return value


def _load_bool_env(name: str, default: bool) -> bool:
    """读取简单布尔配置，避免拼写错误被静默当成 false。"""
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f".env 中的 {name} 必须是 true 或 false")


def _validate_date(value: str, name: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f".env 中的 {name} 必须是 YYYY-MM-DD 日期") from error
    return value


def load_daily_runtime_config(
    env_path: Optional[Path] = None,
) -> DailyRuntimeConfig:
    """从源码或 EXE 同目录读取并严格校验 daily-mode 配置。"""
    config_path = Path(env_path) if env_path else runtime_dir / ".env"
    if not config_path.exists():
        raise FileNotFoundError(
            f"未找到运行配置 {config_path}；请复制 .env.example 为 .env 后填写。"
        )

    # 部署时以 EXE 同目录文件为准，避免机器上遗留的同名系统环境变量覆盖客户配置。
    load_dotenv(config_path, override=True)

    daily_tasks_raw = _load_json_list_env("DAILY_TASKS")
    if not daily_tasks_raw or not all(
        isinstance(task, dict) for task in daily_tasks_raw
    ):
        raise ValueError("DAILY_TASKS 必须是非空的 JSON 对象数组")
    for index, task in enumerate(daily_tasks_raw, start=1):
        try:
            card = int(task.get("card", task.get("task_card_index")))
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"DAILY_TASKS 第 {index} 项缺少有效的 card"
            ) from error
        if card <= 0:
            raise ValueError(f"DAILY_TASKS 第 {index} 项的 card 必须大于 0")
        if task.get("date"):
            _validate_date(str(task["date"]), f"DAILY_TASKS[{index}].date")

    platforms_raw = _load_json_list_env("PLATFORMS")
    if not platforms_raw or not all(
        isinstance(platform, dict) for platform in platforms_raw
    ):
        raise ValueError("PLATFORMS 必须是非空的 JSON 对象数组")
    for index, platform in enumerate(platforms_raw, start=1):
        if not str(platform.get("name", "")).strip():
            raise ValueError(f"PLATFORMS 第 {index} 项缺少 name")
        if not str(platform.get("home_url", "")).strip():
            raise ValueError(f"PLATFORMS 第 {index} 项缺少 home_url")

    markers_raw = _load_json_list_env("GC_PAGE_URL_MARKERS")
    if not markers_raw or not all(
        isinstance(marker, str) and marker.strip() for marker in markers_raw
    ):
        raise ValueError("GC_PAGE_URL_MARKERS 必须是非空字符串数组")

    target_date_raw = (os.getenv("TARGET_DATE") or "").strip()
    target_date = (
        _validate_date(target_date_raw, "TARGET_DATE")
        if target_date_raw
        else None
    )
    task_url = (os.getenv("TASK_URL") or DEFAULT_TASK_URL).strip()
    if not task_url:
        raise ValueError("TASK_URL 不能为空")

    return DailyRuntimeConfig(
        bite_id=_require_env("BITE_ID"),
        worker_count=_load_int_env("WORKER_COUNT", 1),
        max_attempts=_load_int_env("MAX_ATTEMPTS", 1),
        target_date_offset_days=_load_int_env("TARGET_DATE_OFFSET_DAYS", 0),
        target_date=target_date,
        cookie_dir=Path(_require_env("COOKIE_DIR")),
        task_url=task_url,
        daily_tasks=daily_tasks_raw,
        platforms=platforms_raw,
        gc_page_url_markers=[marker.strip() for marker in markers_raw],
        keep_browser_after_run=_load_bool_env("KEEP_BROWSER_AFTER_RUN", True),
    )


class DailyEngine(BackfillEngine):
    """在历史补采业务动作之上增加每日任务的完整外围编排。"""

    def __init__(
        self,
        bite_id: str,
        gc_page_url_markers: Sequence[str],
        worker_count: int = 4,
        max_attempts: int = 5,
        target_date_offset_days: int = 1,
        cookie_dir: Optional[Path] = None,
        task_url: str = DEFAULT_TASK_URL,
        keep_browser_after_run: bool = True,
    ):
        super().__init__(
            bite_id,
            gc_page_url_markers=list(gc_page_url_markers),
        )
        if worker_count <= 0:
            raise ValueError("worker_count 必须大于 0")
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须大于 0")
        if target_date_offset_days < 0:
            raise ValueError("target_date_offset_days 不能小于 0")

        self.worker_count = worker_count
        self.max_attempts = max_attempts
        self.target_date_offset_days = target_date_offset_days
        self.cookie_dir = Path(cookie_dir) if cookie_dir else runtime_dir / "COOKIE"
        self.task_url = task_url
        self.keep_browser_after_run = keep_browser_after_run
        self.browser_manager = BitBrowserManager(bite_id, self.bt_url)
        self.auth_manager = CookieAuthManager(bite_id, self.cookie_dir)

    def build_daily_tasks(
        self,
        tasks_config: Sequence[Dict[str, Any]],
        target_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """每张卡片只生成一个单日任务；重复卡片配置自动去重。"""
        if target_date is None:
            target_date = (
                date.today() - timedelta(days=self.target_date_offset_days)
            ).strftime("%Y-%m-%d")

        tasks: List[Dict[str, Any]] = []
        seen_task_ids = set()
        for config in tasks_config:
            card = int(config.get("card", config.get("task_card_index", 1)))
            task_date = str(config.get("date", target_date))
            task_id = f"card-{card}_{task_date}"
            if task_id in seen_task_ids:
                logger.warning(f"检测到重复每日任务 {task_id}，已跳过。")
                continue
            seen_task_ids.add(task_id)
            tasks.append(
                {
                    "task_id": task_id,
                    "card": card,
                    "start": task_date,
                    "end": task_date,
                    "attempt": 1,
                }
            )

        logger.info(
            f"✓ 每日任务池构建完成：目标日期 {target_date}，"
            f"共 {len(tasks)} 个唯一任务。"
        )
        return tasks

    async def _open_worker_page(
        self,
        context: BrowserContext,
        index: int,
    ) -> Optional[Page]:
        page = await context.new_page()
        try:
            await page.goto(
                self.task_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            ready_card = page.locator(
                "div.workTool_page_card_test_dataCard"
            ).first
            await ready_card.click(trial=True, timeout=90000)
            logger.info(
                f"Worker-页面-{index + 1} 已通过首次任务卡片可操作性检查，"
                "等待 5 秒确认自动登录后的页面状态稳定。"
            )
            await page.wait_for_timeout(5000)
            await ready_card.click(trial=True, timeout=15000)
            logger.info(
                f"✓ Worker-页面-{index + 1} 已完成 datatoolcenter 自动登录和稳定性检查。"
            )
            return page
        except Exception as error:
            logger.error(f"Worker-页面-{index + 1} 打开失败: {error}")
            try:
                await page.close()
            except Exception:
                pass
            return None

    async def create_worker_pages(
        self,
        context: BrowserContext,
        task_count: int,
    ) -> List[Page]:
        """清理旧 Worker，并按任务数量创建不超过配置上限的页面。"""
        stale_worker_pages = [
            page for page in context.pages if "datatoolcenter" in page.url
        ]
        if stale_worker_pages:
            logger.info(
                f"启动前发现 {len(stale_worker_pages)} 个旧 Worker 页面，正在清理。"
            )
            await asyncio.gather(
                *(page.close() for page in stale_worker_pages),
                return_exceptions=True,
            )

        actual_worker_count = min(self.worker_count, task_count)
        opened_pages = await asyncio.gather(
            *(
                self._open_worker_page(context, index)
                for index in range(actual_worker_count)
            )
        )
        worker_pages = [page for page in opened_pages if page is not None]
        logger.info(
            f"Worker 页面创建完成：配置上限 {self.worker_count} 个，"
            f"本次按 {task_count} 个任务计划创建 {actual_worker_count} 个，"
            f"实际可用 {len(worker_pages)} 个。"
        )
        return worker_pages

    async def _daily_worker(
        self,
        page: Page,
        task_queue: asyncio.Queue,
        ledger: TaskLedger,
        list_index: int,
    ) -> bool:
        """持续消费 daily 队列；失败任务未达上限时立即放回队尾。"""
        worker_id = f"页面-{list_index + 1}"
        consecutive_initialization_failures = 0
        logger.info(
            f"Worker-{worker_id} 启动 Daily 持续任务池，绑定页面: {page.url[-25:]}"
        )

        while True:
            task = await task_queue.get()
            if task is None:
                task_queue.task_done()
                logger.info(f"Worker-{worker_id} Daily 任务池已收敛，停止领取。")
                return True

            fatal_error = False
            try:
                success = await self.execute_task(page, task, list_index)
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
                success = False
                fatal_reason = self._fatal_page_error_reason(error)
                if fatal_reason:
                    fatal_error = True
                    logger.error(
                        f"Worker-{worker_id} 因{fatal_reason}停止领取新任务。"
                    )
                else:
                    logger.error(
                        f"Worker-{worker_id} 执行任务时发生未分类异常，"
                        f"当前尝试记为失败: {error}"
                    )

            try:
                await ledger.record(task, success)
                if not success and task["attempt"] < self.max_attempts:
                    retry_task = {**task, "attempt": task["attempt"] + 1}
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
            finally:
                # 重试任务必须先入队再完成当前项，避免 queue.join() 提前返回。
                task_queue.task_done()

            if fatal_error:
                return False

    async def _run_daily_task_pool(
        self,
        tasks: List[Dict[str, Any]],
        worker_pages: List[Page],
        ledger: TaskLedger,
    ) -> None:
        """运行持续任务池，直到每个任务成功或达到各自的尝试上限。"""
        task_queue: asyncio.Queue = asyncio.Queue()
        for task in tasks:
            task_queue.put_nowait(task)

        logger.info(
            f"Daily 持续任务池开始：{len(tasks)} 个任务，"
            f"{len(worker_pages)} 个 Worker，每个任务最多 {self.max_attempts} 次。"
        )
        worker_tasks = [
            asyncio.create_task(
                self._daily_worker(page, task_queue, ledger, index)
            )
            for index, page in enumerate(worker_pages)
        ]
        join_task = asyncio.create_task(task_queue.join())

        while not join_task.done():
            active_workers = [task for task in worker_tasks if not task.done()]
            if not active_workers:
                unprocessed_count = 0
                while True:
                    try:
                        task = task_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        if task is not None:
                            await ledger.record(task, False)
                            unprocessed_count += 1
                    finally:
                        task_queue.task_done()
                if unprocessed_count:
                    logger.error(
                        f"所有 Worker 均已熔断，剩余 {unprocessed_count} 个"
                        "队列任务无法继续执行，已记为失败。"
                    )
                break

            await asyncio.wait(
                [join_task, *active_workers],
                return_when=asyncio.FIRST_COMPLETED,
            )

        await join_task

        # 任务全部到达终态后，用哨兵唤醒仍在等待 queue.get() 的健康 Worker。
        active_workers = [task for task in worker_tasks if not task.done()]
        for _ in active_workers:
            task_queue.put_nowait(None)
        if active_workers:
            await task_queue.join()

        worker_results = await asyncio.gather(
            *worker_tasks,
            return_exceptions=True,
        )
        healthy_count = sum(result is True for result in worker_results)
        for index, result in enumerate(worker_results):
            if isinstance(result, BaseException):
                logger.error(
                    f"Worker-页面-{index + 1} 协程异常退出: {result}"
                )
        logger.info(
            f"Daily 持续任务池结束：{healthy_count}/{len(worker_pages)} 个 Worker "
            "保持健康。"
        )

    @staticmethod
    def _log_auth_report(report: AuthReport) -> None:
        succeeded = "、".join(report.succeeded_platforms) or "无"
        failed = "、".join(report.failed_platforms) or "无"
        log_method = logger.info if report.mode == "NORMAL" else logger.warning
        log_method(
            "登录态重建预检汇总：\n"
            f"  运行状态：{report.mode}\n"
            f"  已登录平台：{succeeded}\n"
            f"  未登录平台：{failed}"
        )

    @staticmethod
    def _log_daily_summary(summary: Dict[str, Any], report: AuthReport) -> None:
        attempt_lines = [
            (
                f"  第 {attempt_result['attempt']} 次尝试统计："
                f"总计 {attempt_result['total']}，"
                f"成功 {attempt_result['success']}，"
                f"失败 {attempt_result['failed']}"
            )
            for attempt_result in summary.get("attempt_stats", [])
        ]
        logger.info(
            "\nDaily-mode 运行汇总：\n"
            f"  登录状态：{report.mode}\n"
            f"  每日任务总数：{summary['total']}\n"
            + ("\n".join(attempt_lines) + "\n" if attempt_lines else "")
            + f"  最终完成：{summary['final_success']}\n"
            f"  最终失败：{summary['final_failed']}\n"
            "  浏览器处理：保留现场，不自动关闭"
        )

    async def run_daily(
        self,
        tasks_config: Sequence[Dict[str, Any]],
        platforms: Sequence[Dict[str, Any]],
        target_date: Optional[str] = None,
    ) -> bool:
        """执行 daily-mode；无论结果如何，结束时均保留比特浏览器。"""
        if not tasks_config or not all(isinstance(item, dict) for item in tasks_config):
            logger.error("tasks_config 必须是非空的 list[dict]。")
            return False
        if not platforms or not all(isinstance(item, dict) for item in platforms):
            logger.error("platforms 必须是非空的 list[dict]。")
            return False

        initial_tasks = self.build_daily_tasks(tasks_config, target_date)
        if not initial_tasks:
            logger.error("配置没有生成任何每日任务。")
            return False

        ledger = TaskLedger(runtime_dir / "daily_results.jsonl")
        await self.browser_manager.close_browser(
            settle_seconds=2.0,
            reason="正在关闭可能遗留的比特浏览器",
        )
        cdp_address = await self.browser_manager.open_browser()
        if not cdp_address:
            return False

        run_succeeded = False
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.connect_over_cdp(
                    f"http://{cdp_address}"
                )
                if not browser.contexts:
                    logger.error("比特浏览器中没有可用的 BrowserContext。")
                    return False
                context = browser.contexts[0]

                # 登录态重建预检早于 GC 挂载；失败平台的诊断页因此可以一直保留。
                auth_report = await self.auth_manager.ensure_platforms(
                    context,
                    platforms,
                )
                self._log_auth_report(auth_report)
                if not auth_report.any_succeeded:
                    logger.error(
                        "全部平台登录态重建失败，daily-mode 不创建任务池；"
                        "浏览器和失败登录页将保留供人工处理。"
                    )
                    return False

                # 从此刻起只监控任务执行期间新产生的商智页面，不扫描登录诊断页。
                context.on("page", self._on_new_page)
                worker_pages = await self.create_worker_pages(
                    context,
                    task_count=len(initial_tasks),
                )
                if not worker_pages:
                    logger.error("没有成功创建任何 Worker 页面，daily-mode 停止。")
                    return False

                # 只有登录预检和 Worker 稳定性检查通过后，才正式开启本轮账本。
                # 这样重复启动关闭旧浏览器时，旧进程写入的失败结果会先完成，
                # 再由真正具备执行条件的新进程统一清空。
                try:
                    await ledger.reset()
                except Exception as error:
                    logger.error(f"无法创建或覆盖 daily 任务账本: {error}")
                    return False

                logger.info(
                    f"Worker 已稳定，本轮 Daily 任务账本已重置: {ledger.path}；"
                    f"日志继续追加到: {log_path}"
                )

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
                    await self._run_daily_task_pool(
                        initial_tasks,
                        worker_pages,
                        ledger,
                    )

                    summary = await ledger.summary(total_tasks=len(initial_tasks))
                    self._log_daily_summary(summary, auth_report)
                    run_succeeded = summary["final_failed"] == 0
                finally:
                    await self._stop_error_toast_monitors(error_toast_monitors)
        except Exception as error:
            logger.exception(f"daily-mode 主流程发生异常: {error}")
            return False
        finally:
            if run_succeeded and not self.keep_browser_after_run:
                await self.browser_manager.close_browser(
                    reason="Daily 全部任务成功，正在按配置关闭比特浏览器",
                )
            elif run_succeeded:
                logger.info(
                    "Daily 全部任务成功；KEEP_BROWSER_AFTER_RUN=true，"
                    "保留比特浏览器和页面现场。"
                )
            else:
                logger.info(
                    "daily-mode 未全部成功或未进入有效任务阶段，"
                    "保留比特浏览器和页面现场供人工检查。"
                )

        return run_succeeded


if __name__ == "__main__":
    try:
        config = load_daily_runtime_config()
    except (OSError, ValueError) as error:
        logger.error(f"daily-mode 运行配置加载失败: {error}")
        sys.exit(1)

    engine = DailyEngine(
        bite_id=config.bite_id,
        gc_page_url_markers=config.gc_page_url_markers,
        worker_count=config.worker_count,
        max_attempts=config.max_attempts,
        target_date_offset_days=config.target_date_offset_days,
        cookie_dir=config.cookie_dir,
        task_url=config.task_url,
        keep_browser_after_run=config.keep_browser_after_run,
    )
    success = asyncio.run(
        engine.run_daily(
            config.daily_tasks,
            config.platforms,
            target_date=config.target_date,
        )
    )
    sys.exit(0 if success else 1)
