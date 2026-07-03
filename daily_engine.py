#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RPA 每日任务调度入口：登录预检、固定 Worker、共享任务池和多轮重试。"""

import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# 必须在导入 backfill_engine 前指定，避免 daily 日志写入历史补采日志。
os.environ.setdefault("RPA_LOG_FILENAME", "daily_run.log")
# daily-mode 默认只写日志文件；即使源码从终端运行也不输出控制台日志。
os.environ.setdefault("RPA_CONSOLE_LOGGING", "0")

from playwright.async_api import BrowserContext, Page, async_playwright

from auth_manager import AuthReport, CookieAuthManager
from backfill_engine import BackfillEngine, logger, log_path, runtime_dir
from browser_manager import BitBrowserManager
from task_ledger import TaskLedger


class DailyEngine(BackfillEngine):
    """在历史补采业务动作之上增加每日任务的完整外围编排。"""

    def __init__(
        self,
        bite_id: str,
        worker_count: int = 4,
        max_attempts: int = 5,
        target_date_offset_days: int = 1,
        cookie_dir: Optional[Path] = None,
        task_url: str = (
            "https://datatoolcenter.com/web/dateCenter.html?"
            "activeName=selfitemkeyShop&menuplat=%E5%B7%A5%E4%BD%9C%E5%8F%B0"
        ),
    ):
        super().__init__(bite_id)
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

    @staticmethod
    def _log_auth_report(report: AuthReport) -> None:
        succeeded = "、".join(report.succeeded_platforms) or "无"
        failed = "、".join(report.failed_platforms) or "无"
        log_method = logger.info if report.mode == "NORMAL" else logger.warning
        log_method(
            "登录预检汇总：\n"
            f"  运行状态：{report.mode}\n"
            f"  已登录平台：{succeeded}\n"
            f"  未登录平台：{failed}"
        )

    @staticmethod
    def _log_daily_summary(summary: Dict[str, Any], report: AuthReport) -> None:
        round_lines = [
            (
                f"  第 {round_result['attempt']} 轮："
                f"总计 {round_result['total']}，"
                f"成功 {round_result['success']}，"
                f"失败 {round_result['failed']}"
            )
            for round_result in summary.get("rounds", [])
        ]
        logger.info(
            "\nDaily-mode 运行汇总：\n"
            f"  登录状态：{report.mode}\n"
            f"  每日任务总数：{summary['total']}\n"
            + ("\n".join(round_lines) + "\n" if round_lines else "")
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
        try:
            await ledger.reset()
        except Exception as error:
            logger.error(f"无法创建或覆盖 daily 任务账本: {error}")
            return False

        logger.info(
            f"Daily 任务账本已重置: {ledger.path}；"
            f"日志继续追加到: {log_path}"
        )

        await self.browser_manager.close_before_start()
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

                # 登录预检早于 GC 挂载；失败平台的诊断页因此可以一直保留。
                auth_report = await self.auth_manager.ensure_platforms(
                    context,
                    platforms,
                )
                self._log_auth_report(auth_report)
                if not auth_report.any_succeeded:
                    logger.error(
                        "全部平台登录预检失败，daily-mode 不创建任务池；"
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

                error_toast_monitors = [
                    asyncio.create_task(
                        self._monitor_worker_error_toasts(
                            page,
                            f"页面-{index + 1}",
                        )
                    )
                    for index, page in enumerate(worker_pages)
                ]

                healthy_pages = worker_pages
                current_tasks = initial_tasks
                try:
                    for attempt in range(1, self.max_attempts + 1):
                        round_name = (
                            "首次执行" if attempt == 1 else f"第 {attempt} 轮重试"
                        )
                        healthy_pages = await self._run_task_round(
                            current_tasks,
                            healthy_pages,
                            ledger,
                            round_name,
                        )

                        if attempt >= self.max_attempts:
                            break

                        failed_tasks = await ledger.failed_tasks(attempt=attempt)
                        if not failed_tasks:
                            logger.info(
                                f"第 {attempt} 轮后已无失败任务，提前结束重试。"
                            )
                            break
                        if not healthy_pages:
                            logger.error(
                                "所有 Worker 均已失效，无法继续执行后续重试轮次。"
                            )
                            break

                        logger.warning(
                            f"第 {attempt} 轮结束后仍有 {len(failed_tasks)} 个失败任务，"
                            f"准备进入第 {attempt + 1} 轮。"
                        )
                        current_tasks = failed_tasks

                    summary = await ledger.summary(total_tasks=len(initial_tasks))
                    self._log_daily_summary(summary, auth_report)
                    run_succeeded = summary["final_failed"] == 0
                finally:
                    await self._stop_error_toast_monitors(error_toast_monitors)
        except Exception as error:
            logger.exception(f"daily-mode 主流程发生异常: {error}")
            return False
        finally:
            logger.info(
                "daily-mode 已结束；按照当前策略保留比特浏览器和页面现场，"
                "不会执行结束关闭。"
            )

        return run_succeeded


if __name__ == "__main__":
    # 当前 backfill 项目的 daily-mode 配置；部署其他客户时只修改本区域。
    BITE_ID = "4626a1f1fadb4ac4aab182d93469147f"
    WORKER_COUNT = 4
    MAX_ATTEMPTS = 5
    TARGET_DATE_OFFSET_DAYS = 1
    COOKIE_DIR = Path(r"C:\Users\Administrator\Desktop\COOKIE")

    DAILY_TASKS = [
        {"card": 2},
        {"card": 3},
        {"card": 4},
        {"card": 5},
        {"card": 6}
    ]

    PLATFORMS = [
        {
            "name": "京东品牌主页（商智）",
            "home_url": "https://ppzh.jd.com/brand/homePage/index.html",
            "login_url_markers": ["login"],
            "cookie_key": "京东品牌主页",
            "file_prefix": "jd_cookies",
            "cookie_enabled": True,
        },
    ]

    engine = DailyEngine(
        bite_id=BITE_ID,
        worker_count=WORKER_COUNT,
        max_attempts=MAX_ATTEMPTS,
        target_date_offset_days=TARGET_DATE_OFFSET_DAYS,
        cookie_dir=COOKIE_DIR,
    )
    success = asyncio.run(engine.run_daily(DAILY_TASKS, PLATFORMS))
    sys.exit(0 if success else 1)
