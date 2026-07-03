#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发任务结果账本：将每次任务尝试的最终结果串行写入 JSONL。"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List


logger = logging.getLogger("BackfillEngine")


class TaskLedger:
    """多个 Worker 共享的极简 JSONL 结果账本。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    async def reset(self) -> None:
        """清空上一次运行结果，为本轮运行创建全新账本。"""
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")

    async def record(self, task: Dict[str, Any], success: bool) -> Dict[str, Any]:
        """把一次任务尝试的最终结果追加为一行完整 JSON。"""
        record = {
            "task_id": task["task_id"],
            "card": task["card"],
            "start": task["start"],
            "end": task["end"],
            "attempt": task["attempt"],
            "success": bool(success),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))

        async with self._lock:
            with self.path.open("a", encoding="utf-8") as ledger_file:
                ledger_file.write(line + "\n")
                ledger_file.flush()

        return record

    async def load(self) -> List[Dict[str, Any]]:
        """读取本轮全部有效记录；损坏行写入日志并跳过。"""
        async with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()

        records: List[Dict[str, Any]] = []
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                logger.warning(
                    f"任务账本第 {line_number} 行不是有效 JSON，已跳过: {error}"
                )
        return records

    async def failed_tasks(self, attempt: int) -> List[Dict[str, Any]]:
        """读取指定轮次失败项，并生成下一轮可以直接入队的任务。"""
        records = await self.load()
        latest_by_task: Dict[str, Dict[str, Any]] = {}

        for record in records:
            if record.get("attempt") == attempt:
                latest_by_task[record["task_id"]] = record

        return [
            {
                "task_id": record["task_id"],
                "card": record["card"],
                "start": record["start"],
                "end": record["end"],
                "attempt": attempt + 1,
            }
            for record in latest_by_task.values()
            if record.get("success") is False
        ]

    async def summary(self, total_tasks: int) -> Dict[str, Any]:
        """按 task_id 的最新尝试结果生成逐轮及最终统计。"""
        records = await self.load()
        first_results: Dict[str, Dict[str, Any]] = {}
        retry_results: Dict[str, Dict[str, Any]] = {}
        latest_results: Dict[str, Dict[str, Any]] = {}
        results_by_attempt: Dict[int, Dict[str, Dict[str, Any]]] = {}

        for record in records:
            task_id = record["task_id"]
            attempt = record.get("attempt", 0)
            if isinstance(attempt, int) and attempt > 0:
                results_by_attempt.setdefault(attempt, {})[task_id] = record
            if attempt == 1:
                first_results[task_id] = record
            elif attempt == 2:
                retry_results[task_id] = record

            previous = latest_results.get(task_id)
            if previous is None or attempt >= previous.get("attempt", 0):
                latest_results[task_id] = record

        first_success = sum(
            record.get("success") is True for record in first_results.values()
        )
        retry_success = sum(
            record.get("success") is True for record in retry_results.values()
        )
        final_success = sum(
            record.get("success") is True for record in latest_results.values()
        )
        rounds = []
        for attempt in sorted(results_by_attempt):
            attempt_results = results_by_attempt[attempt]
            success_count = sum(
                record.get("success") is True
                for record in attempt_results.values()
            )
            rounds.append(
                {
                    "attempt": attempt,
                    "total": len(attempt_results),
                    "success": success_count,
                    "failed": len(attempt_results) - success_count,
                }
            )

        return {
            "total": total_tasks,
            "first_success": first_success,
            "first_failed": total_tasks - first_success,
            "retry_total": len(retry_results),
            "retry_success": retry_success,
            "retry_failed": len(retry_results) - retry_success,
            "final_success": final_success,
            "final_failed": total_tasks - final_success,
            "rounds": rounds,
            "attempts_run": len(rounds),
        }
