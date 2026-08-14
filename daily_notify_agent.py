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


def expected_tasks(client_env: dict[str, str]) -> dict[str, dict[str, int]]:
    """按 Dailyfill Launcher 配置还原本次任务及其展示信息。"""
    tasks = json.loads(client_env.get("DAILY_TASKS", "[]"))
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("DAILY_TASKS 必须是非空 JSON 数组")

    expected: dict[str, dict[str, int]] = {}
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise ValueError(f"DAILY_TASKS 第 {index} 项必须是 JSON 对象")
        try:
            card_id = int(task["card_id"])
            offset_days = int(task["target_date_offset_days"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"DAILY_TASKS 第 {index} 项必须包含有效的 "
                "card_id 和 target_date_offset_days"
            ) from error
        if card_id <= 0:
            raise ValueError(f"DAILY_TASKS 第 {index} 项的 card_id 必须大于 0")
        if offset_days < 0:
            raise ValueError(
                f"DAILY_TASKS 第 {index} 项的 target_date_offset_days 不能小于 0"
            )

        task_date = (date.today() - timedelta(days=offset_days)).strftime(
            "%Y-%m-%d"
        )
        task_id = f"card-{card_id}_{task_date}"
        expected[task_id] = {
            "card_id": card_id,
            "target_date_offset_days": offset_days,
        }
    return expected


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


def read_run_status(path: Path) -> dict[str, Any] | None:
    """读取 DailyEngine 当前运行状态；旧版实例没有该文件时返回 None。"""
    if not path.exists():
        return None

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path.name} 不是有效 JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 必须是 JSON 对象")

    run_date = str(value.get("run_date", "")).strip()
    if not run_date:
        raise ValueError(f"{path.name} 缺少 run_date")
    try:
        date.fromisoformat(run_date)
    except ValueError as error:
        raise ValueError(f"{path.name} 的 run_date 不是有效日期") from error

    phase = str(value.get("phase", "")).strip().lower()
    if phase not in {"running", "finished"}:
        raise ValueError(f"{path.name} 的 phase 必须是 running 或 finished")

    auth_mode = str(value.get("auth_mode", "NOT_CHECKED")).strip().upper()
    if auth_mode not in {"NOT_CHECKED", "NORMAL", "DEGRADED", "AUTH_REQUIRED"}:
        raise ValueError(f"{path.name} 的 auth_mode 无效: {auth_mode}")

    auth_results = value.get("auth_results", {})
    if not isinstance(auth_results, dict):
        raise ValueError(f"{path.name} 的 auth_results 必须是 JSON 对象")

    return {
        **value,
        "run_date": run_date,
        "phase": phase,
        "ledger_reset": value.get("ledger_reset") is True,
        "auth_mode": auth_mode,
        "auth_results": {
            str(name): succeeded is True
            for name, succeeded in auth_results.items()
        },
    }


def append_note(note: str, addition: str) -> str:
    return f"{note}；{addition}" if note else addition


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

    expected = expected_tasks(client_env)
    run_status = read_run_status(client_dir / config["status_file"])
    is_current_run = (
        run_status is not None
        and run_status["run_date"] == date.today().isoformat()
    )
    # 没有状态文件时兼容尚未升级的 DailyEngine；一旦存在新版状态文件，
    # 只有本日运行且已经 reset 的账本才属于当前批次。
    ledger_available = run_status is None or (
        is_current_run and run_status["ledger_reset"]
    )
    results = (
        latest_jsonl_results(client_dir / config["results_file"])
        if ledger_available
        else {}
    )
    matched = {
        task_id: record
        for task_id, record in results.items()
        if task_id in expected
    }

    success_count = sum(record.get("success") is True for record in matched.values())
    total_count = len(expected)
    max_attempts = int(client_env.get("MAX_ATTEMPTS", "1"))
    phase = run_status["phase"] if is_current_run else None
    auth_mode = run_status["auth_mode"] if is_current_run else "NOT_CHECKED"
    auth_results = run_status["auth_results"] if is_current_run else {}
    failed_platforms = [
        name for name, succeeded in auth_results.items() if not succeeded
    ]

    task_details = []
    for task_id, task in expected.items():
        record = matched.get(task_id)
        if record is None:
            task_status = "未完成" if phase == "finished" and ledger_available else "暂无结果"
            attempt = None
            task_name = None
            missing_count = None
        elif record.get("success") is True:
            task_status = "完成"
            attempt = int(record.get("attempt", 0))
            task_name = record.get("task_name")
            missing_count = record.get("missing_count")
        else:
            attempt = int(record.get("attempt", 0))
            task_status = (
                "重试中"
                if attempt < max_attempts and phase != "finished"
                else "未完成"
            )
            task_name = record.get("task_name")
            missing_count = record.get("missing_count")

        task_details.append(
            {
                **task,
                "task_name": task_name,
                "status": task_status,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "missing_count": missing_count,
            }
        )

    note = ""
    if run_status is not None and not is_current_run:
        status = "未开始"
        note = "今日 DailyEngine 尚未启动"
    elif auth_mode == "AUTH_REQUIRED":
        status = "登录异常"
        note = "全部平台登录失效，任务未启动"
    elif is_current_run and not run_status["ledger_reset"]:
        if phase == "running":
            status = "运行中"
            note = "任务账本尚未重置"
        else:
            status = "未完成"
            note = "本轮未进入任务执行阶段"
    elif not matched:
        if phase == "finished":
            status = "未完成"
            note = "本轮未产生任务结果"
        else:
            status = "未开始"
            note = "未发现今日账本记录"
    elif success_count >= total_count:
        status = "完成"
    elif phase == "finished":
        status = "未完成"
    else:
        status = "运行中"

    if failed_platforms and auth_mode != "AUTH_REQUIRED":
        note = append_note(note, f"登录失效：{'、'.join(failed_platforms)}")

    if status != "完成" and phase != "finished" and not (
        run_status is not None and not is_current_run
    ):
        log_age = minutes_since_modified(client_dir / config["log_file"])
        if log_age is None:
            note = append_note(note, "未发现 log")
        elif log_age >= config["stale_log_minutes"]:
            warning = f"log {log_age} 分钟未更新，疑似故障"
            note = append_note(note, warning)

    return {
        "customer": customer_name,
        "status": status,
        "success": success_count,
        "total": total_count,
        "note": note,
        "tasks": task_details,
        "failed_platforms": failed_platforms,
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
            "tasks": [],
            "failed_platforms": [],
        }


def progress_line(item: dict[str, Any]) -> str:
    needs_attention = (
        item["status"] in ("配置异常", "登录异常", "未完成")
        or bool(item.get("failed_platforms"))
        or "疑似故障" in item["note"]
    )
    if needs_attention:
        emoji = "⚠️"
    elif item["status"] == "完成":
        emoji = "✅"
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
    attention = sum(
        item["status"] in ("配置异常", "登录异常", "未完成")
        or bool(item.get("failed_platforms"))
        or "疑似故障" in item["note"]
        for item in items
    )

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


def task_detail_line(task: dict[str, Any]) -> str:
    status = task["status"]
    if status == "完成":
        emoji = "✅"
    elif status in ("重试中", "未完成"):
        emoji = "⏳"
    else:
        emoji = "⚪"

    task_label = (
        f"{task['task_name']}（ID: {task['card_id']}）"
        if task.get("task_name")
        else f"任务 {task['card_id']}"
    )
    offset = task["target_date_offset_days"]
    date_label = "当天" if offset == 0 else f"前{offset}天"
    line = f"{emoji} {task_label}｜{date_label}｜{status}"
    if status in ("重试中", "未完成"):
        if task["attempt"] is not None:
            line += f"｜第{task['attempt']}/{task['max_attempts']}次"
        missing_count = task.get("missing_count")
        if isinstance(missing_count, int) and missing_count > 0:
            line += f"｜剩余缺失 {missing_count} 条"
    return line


def build_card(items: list[dict[str, Any]], title: str) -> dict[str, Any]:
    """构建目录树样式的客户任务折叠卡片。"""
    done = sum(item["status"] == "完成" for item in items)
    running = sum(item["status"] == "运行中" for item in items)
    not_started = sum(item["status"] == "未开始" for item in items)
    attention = sum(
        item["status"] in ("配置异常", "登录异常", "未完成")
        or bool(item.get("failed_platforms"))
        or "疑似故障" in item["note"]
        for item in items
    )

    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**汇总：** 完成 {done}｜运行中 {running}｜"
                f"未开始 {not_started}｜需关注 {attention}"
            ),
        }
    ]

    for index, item in enumerate(items, start=1):
        detail_lines = [
            f"❌ {platform_name}｜登录失效"
            for platform_name in item.get("failed_platforms", [])
        ]
        detail_lines.extend(task_detail_line(task) for task in item["tasks"])
        if not detail_lines:
            detail_lines.append(item["note"] or "暂无任务详情")

        elements.append(
            {
                "tag": "collapsible_panel",
                "element_id": f"customer_{index}",
                "expanded": False,
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": progress_line(item),
                    },
                    "width": "auto_when_fold",
                    "vertical_align": "center",
                    "icon": {
                        "tag": "standard_icon",
                        "token": "down-small-ccm_outlined",
                        "size": "16px 16px",
                    },
                    "icon_position": "left",
                    "icon_expanded_angle": -180,
                    "padding": "2px 0px 2px 0px",
                },
                "margin": "0px",
                "vertical_spacing": "2px",
                "padding": "2px 0px 4px 24px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "\n".join(detail_lines),
                    }
                ],
            }
        )

    if not items:
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    "当前没有到达 REPORT_READY_TIME 的客户任务，"
                    "或未发现客户 .env。"
                ),
            }
        )

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"【{title}】{datetime.now():%Y-%m-%d %H:%M}",
            },
            "template": "blue",
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "4px",
            "padding": "8px 12px 10px 12px",
            "elements": elements,
        },
    }


def send_feishu(card: dict[str, Any], webhook_url: str) -> None:
    if not webhook_url:
        raise ValueError("notify_agent.env 缺少 FEISHU_WEBHOOK_URL")

    response = requests.post(
        webhook_url,
        json={"msg_type": "interactive", "card": card},
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
    card = build_card(items, config["title"])
    send_feishu(card, config["webhook_url"])
    message = build_message(items, config["title"])
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
        "status_file": raw.get("DAILY_STATUS_FILENAME", "daily_run_status.json"),
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
