#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily-mode 飞书巡检通知器。

设计目标：简单、直观、少抽象。

它只做三件事：
1. 递归扫描 dailyfill 下每个客户目录里的 .env；
2. 到达客户 .env 中的 REPORT_READY_TIME 后，读取 jsonl/log 计算进度；
3. 定时发送一条飞书汇总消息。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values


RUNTIME_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)

logger = logging.getLogger("DailyNotifyAgent")


def setup_logging(log_dir: Path) -> None:
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(
        log_dir / "daily_notify_agent.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def read_env(path: Path) -> dict[str, str]:
    return {
        key: value
        for key, value in dotenv_values(path).items()
        if key and value is not None
    }


def parse_time(value: str) -> tuple[int, int]:
    parsed = datetime.strptime(value.strip(), "%H:%M")
    return parsed.hour, parsed.minute


def today_at(hour: int, minute: int) -> datetime:
    return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)


def minutes_since_modified(path: Path) -> int | None:
    if not path.exists():
        return None
    modified_at = datetime.fromtimestamp(path.stat().st_mtime)
    return max(0, math.floor((datetime.now() - modified_at).total_seconds() / 60))


def expected_task_ids(client_env: dict[str, str]) -> set[str]:
    """按 daily_engine.py 的规则还原本次应该出现的 task_id。"""
    tasks = json.loads(client_env.get("DAILY_TASKS", "[]"))
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("DAILY_TASKS 必须是非空 JSON 数组")

    target_date = client_env.get("TARGET_DATE", "").strip()
    if not target_date:
        offset_days = int(client_env.get("TARGET_DATE_OFFSET_DAYS", "1"))
        target_date = (date.today() - timedelta(days=offset_days)).strftime("%Y-%m-%d")

    task_ids: set[str] = set()
    for task in tasks:
        card = int(task.get("card", task.get("task_card_index", 1)))
        task_date = str(task.get("date", target_date))
        task_ids.add(f"card-{card}_{task_date}")
    return task_ids


def latest_jsonl_results(path: Path) -> dict[str, dict[str, Any]]:
    """读取 jsonl，并保留每个 task_id 最新 attempt。"""
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        task_id = record.get("task_id")
        if not task_id:
            continue

        attempt = int(record.get("attempt", 0))
        old_attempt = int(latest.get(task_id, {}).get("attempt", -1))
        if attempt >= old_attempt:
            latest[task_id] = record

    return latest


def inspect_client(env_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """巡检一个客户目录；未到 REPORT_READY_TIME 时返回 None。"""
    client_dir = env_path.parent
    client_env = read_env(env_path)
    customer_name = client_env.get("CUSTOMER_NAME", "").strip() or client_dir.name

    ready_time = client_env.get("REPORT_READY_TIME", "").strip()
    if not ready_time:
        raise ValueError("缺少 REPORT_READY_TIME")
    ready_at = today_at(*parse_time(ready_time))
    if datetime.now() < ready_at:
        return None

    task_ids = expected_task_ids(client_env)
    results = latest_jsonl_results(client_dir / config["results_file"])
    matched = {
        task_id: record
        for task_id, record in results.items()
        if task_id in task_ids
    }

    success_count = sum(record.get("success") is True for record in matched.values())
    total_count = len(task_ids)

    if not matched:
        status = "未开始"
        note = "未发现今日账本记录"
    elif success_count >= total_count:
        status = "完成"
        note = ""
    else:
        status = "运行中"
        note = ""

    if status != "完成":
        log_age = minutes_since_modified(client_dir / config["log_file"])
        if log_age is None:
            note = f"{note}；未发现 log" if note else "未发现 log"
        elif log_age >= config["stale_log_minutes"]:
            warning = f"log {log_age} 分钟未更新，疑似故障"
            note = f"{note}；{warning}" if note else warning

    return {
        "customer": customer_name,
        "status": status,
        "success": success_count,
        "total": total_count,
        "note": note,
    }


def inspect_client_safe(env_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return inspect_client(env_path, config)
    except Exception as error:
        return {
            "customer": env_path.parent.name,
            "status": "配置异常",
            "success": 0,
            "total": 0,
            "note": str(error),
        }


def progress_line(item: dict[str, Any]) -> str:
    if item["status"] == "完成":
        emoji = "✅"
    elif item["status"] == "配置异常" or "疑似故障" in item["note"]:
        emoji = "⚠️"
    elif item["status"] == "运行中":
        emoji = "⏳"
    else:
        emoji = "⚪"

    line = f"{emoji} {item['customer']}｜{item['status']}｜{item['success']}/{item['total']}"
    if item["note"]:
        line += f"｜{item['note']}"
    return line


def build_message(items: list[dict[str, Any]], title: str) -> str:
    done = sum(item["status"] == "完成" for item in items)
    running = sum(item["status"] == "运行中" for item in items)
    not_started = sum(item["status"] == "未开始" for item in items)
    attention = sum(item["status"] == "配置异常" or "疑似故障" in item["note"] for item in items)

    lines = [
        f"【{title}】{datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"汇总：完成 {done}｜运行中 {running}｜未开始 {not_started}｜需关注 {attention}",
        "",
    ]
    if items:
        lines.extend(progress_line(item) for item in items)
    else:
        lines.append("当前没有到达 REPORT_READY_TIME 的客户任务，或未发现客户 .env。")
    return "\n".join(lines)


def send_feishu(message: str, webhook_url: str) -> None:
    if not webhook_url:
        raise ValueError("notify_agent.env 缺少 FEISHU_WEBHOOK_URL")

    response = requests.post(
        webhook_url,
        json={"msg_type": "text", "content": {"text": message}},
        timeout=10,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("code", 0) not in (0, None):
        raise RuntimeError(f"飞书通知返回异常: {result}")


def run_once(config: dict[str, Any]) -> None:
    env_files = sorted(config["clients_root"].rglob(".env"))
    items = [
        item
        for env_path in env_files
        if (item := inspect_client_safe(env_path, config)) is not None
    ]
    message = build_message(items, config["title"])
    send_feishu(message, config["webhook_url"])
    logger.info("飞书通知发送成功。\n%s", message)


def next_notify_time(config: dict[str, Any]) -> datetime:
    now = datetime.now()
    start_at = today_at(*config["start_time"])
    end_at = today_at(*config["end_time"])
    interval = timedelta(minutes=config["interval_minutes"])

    if now < start_at:
        return start_at
    if now > end_at:
        return start_at + timedelta(days=1)

    slot = math.ceil((now - start_at) / interval)
    notify_at = start_at + slot * interval
    if notify_at <= now:
        notify_at += interval
    if notify_at > end_at:
        return start_at + timedelta(days=1)
    return notify_at


def run_forever(config: dict[str, Any]) -> None:
    logger.info(
        "通知器启动：clients_root=%s, window=%02d:%02d-%02d:%02d, interval=%smin",
        config["clients_root"],
        *config["start_time"],
        *config["end_time"],
        config["interval_minutes"],
    )
    while True:
        notify_at = next_notify_time(config)
        logger.info("下一次通知时间：%s", notify_at.strftime("%Y-%m-%d %H:%M:%S"))
        while datetime.now() < notify_at:
            time.sleep(30)

        try:
            run_once(config)
        except Exception as error:
            logger.exception("巡检通知失败: %s", error)


def load_config(path: Path) -> dict[str, Any]:
    raw = read_env(path)
    base_dir = path.resolve().parent
    clients_root = Path(raw.get("CLIENTS_ROOT", "").strip() or base_dir)
    if not clients_root.is_absolute():
        clients_root = base_dir / clients_root

    return {
        "config_dir": base_dir,
        "clients_root": clients_root.resolve(),
        "webhook_url": raw.get("FEISHU_WEBHOOK_URL", "").strip(),
        "title": raw.get("NOTIFY_TITLE", "Daily RPA 巡检").strip() or "Daily RPA 巡检",
        "start_time": parse_time(raw.get("NOTIFY_START_TIME", "09:00")),
        "end_time": parse_time(raw.get("NOTIFY_END_TIME", "18:00")),
        "interval_minutes": int(raw.get("NOTIFY_INTERVAL_MINUTES", "30")),
        "stale_log_minutes": int(raw.get("STALE_LOG_MINUTES", "20")),
        "results_file": raw.get("DAILY_RESULTS_FILENAME", "daily_results.jsonl"),
        "log_file": raw.get("DAILY_LOG_FILENAME", "daily_run.log"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="daily-mode 飞书巡检通知器")
    parser.add_argument("--config", type=Path, default=RUNTIME_DIR / "notify_agent.env")
    parser.add_argument("--once", action="store_true", help="只发送一次，然后退出")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        setup_logging(config["config_dir"])
        if args.once:
            run_once(config)
        else:
            run_forever(config)
        return 0
    except KeyboardInterrupt:
        logger.info("收到中断信号，通知器退出。")
        return 130
    except Exception as error:
        setup_logging(RUNTIME_DIR)
        logger.exception("通知器启动失败: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
