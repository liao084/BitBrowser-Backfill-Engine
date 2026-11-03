#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按完整自然周执行历史补采，复用 Backfill 调度和已验证的周日期注入。

沿用 Backfill GUI 生成的 .env（TASKS_CONFIG 使用 card/start/end）。
只采完整落在配置区间内、且不晚于上周的周，每个区间按由近到远排列。
chunk_days 不参与周拆分，每个任务固定为一个周一至周日。
浏览器连接、已有 Worker、重试、CDP 轮换、GC 和账本均沿用 Backfill。
运行前需保留已处于周维度的 Worker 页面，例如日常周采集结束后留下的页面。
源码运行：python backfill_engine_by_week.py。打包时使用本文件作为入口。
"""

import asyncio
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List

# 导入日常周模块之前确定日志配置，保持 Backfill 的文件和控制台日志行为。
os.environ.setdefault("RPA_LOG_FILENAME", "backfill_run.log")
os.environ.setdefault("RPA_CONSOLE_LOGGING", "1")

from backfill_engine import (
    BackfillEngine,
    load_runtime_config,
    logger,
)
from browser_connector import BitBrowserConnector, ExternalCdpConnector
from daily_engine_by_week import WeeklyEngine, previous_week_range
from github_info import GIT_SHA


def generate_week_chunks(
    start_date: str,
    end_date: str,
    latest_sunday: date,
) -> List[tuple[str, str]]:
    """返回区间内完整且可采的周一/周日组合，最近一周优先。"""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start > end:
        raise ValueError(f"周补采开始日期不能晚于结束日期：{start_date} 至 {end_date}")

    # 先截到最晚可采日期，再向前对齐周日，首尾均不扩出配置区间。
    week_end = min(end, latest_sunday)
    week_end -= timedelta(days=(week_end.weekday() + 1) % 7)
    week_start = week_end - timedelta(days=6)
    chunks: List[tuple[str, str]] = []
    while week_start >= start:
        chunks.append((week_start.isoformat(), week_end.isoformat()))
        week_start -= timedelta(days=7)
        week_end -= timedelta(days=7)
    return chunks


class WeeklyBackfillEngine(BackfillEngine):
    """保留历史补采总控，仅适配周任务生成和日期注入。"""

    # 直接复用同一组方法，不调用 DailyEngine 的构造函数或浏览器登录编排。
    _inject_week_date = WeeklyEngine._inject_week_date
    _initialize_week_range = WeeklyEngine._initialize_week_range
    inject_dates = WeeklyEngine.inject_dates

    def build_tasks(self, tasks_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """先展开为完整周配置，再复用父类生成任务字段并去重。"""
        latest_sunday = date.fromisoformat(previous_week_range(date.today())[1])
        weekly_configs: List[Dict[str, Any]] = []
        for config in tasks_list:
            card = int(config.get("card", config.get("task_card_index", 1)))
            weeks = generate_week_chunks(config["start"], config["end"], latest_sunday)
            if not weeks:
                logger.warning(
                    "任务卡片 %s 的区间 %s 至 %s 内没有完整可采周，已跳过。",
                    card,
                    config["start"],
                    config["end"],
                )
            for start_date, end_date in weeks:
                weekly_configs.append(
                    {
                        "card": card,
                        "start": start_date,
                        "end": end_date,
                        "chunk_days": 7,
                    }
                )

        tasks = super().build_tasks(weekly_configs)
        logger.info(
            "周补采任务池：最晚可采周日 %s，共 %s 个唯一完整周任务；忽略 chunk_days。",
            latest_sunday,
            len(tasks),
        )
        return tasks


def main() -> int:
    logger.info("程序版本: app=backfill_engine_by_week, git_sha=%s", GIT_SHA)
    try:
        config = load_runtime_config()
    except (OSError, ValueError) as error:
        logger.error("周补采配置加载失败: %s", error)
        return 1

    if config.browser_type == "bitbrowser":
        connector = BitBrowserConnector(config.bite_id or "")
    else:
        connector = ExternalCdpConnector(config.cdp_address or "")

    engine = WeeklyBackfillEngine(
        config.bite_id,
        gc_page_url_markers=config.gc_page_url_markers,
        browser_connector=connector,
        worker_heartbeat_silence_seconds=config.worker_heartbeat_silence_seconds,
        business_heartbeat_silence_seconds=config.business_heartbeat_silence_seconds,
        max_attempts=config.max_attempts,
        cdp_session_lifetime_hours=config.cdp_session_lifetime_hours,
        max_cdp_rebuilds=config.max_cdp_rebuilds,
    )
    asyncio.run(engine.run(config.tasks_config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
