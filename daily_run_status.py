#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DailyEngine 当前运行状态：用原子 JSON 替换向旁路通知器提供状态。"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4


logger = logging.getLogger("BackfillEngine")


class DailyRunStatus:
    """维护一份只表示最新 Daily 运行的状态文件。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.started_at = datetime.now().astimezone()
        self.run_id = (
            self.started_at.strftime("%Y%m%dT%H%M%S%f")
            + "-"
            + uuid4().hex[:8]
        )

    @staticmethod
    def _iso_now() -> str:
        return datetime.now().astimezone().isoformat()

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """跨进程串行化状态文件的读取、所有权校验和替换。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"读取 Daily 运行状态失败，将按无状态处理: {error}")
            return None
        return value if isinstance(value, dict) else None

    def _write_unlocked(self, state: Mapping[str, Any]) -> None:
        temporary_path = self.path.with_name(
            f"{self.path.name}.{self.run_id}.tmp"
        )
        content = json.dumps(
            dict(state),
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        temporary_path.write_text(content, encoding="utf-8")
        os.replace(temporary_path, self.path)

    def start(self) -> None:
        """声明本轮为当前运行；新进程允许主动取得状态文件所有权。"""
        state = {
            "run_id": self.run_id,
            "run_date": self.started_at.date().isoformat(),
            "started_at": self.started_at.isoformat(),
            "updated_at": self.started_at.isoformat(),
            "phase": "running",
            "ledger_reset": False,
            "auth_mode": "NOT_CHECKED",
            "auth_results": {},
        }
        with self._exclusive_lock():
            self._write_unlocked(state)

    def update(self, **changes: Any) -> bool:
        """仅当本轮仍拥有状态文件时更新；旧进程不得覆盖新进程。"""
        with self._exclusive_lock():
            state = self._read_unlocked()
            if state is None or state.get("run_id") != self.run_id:
                current_run_id = state.get("run_id") if state else "无"
                logger.warning(
                    "跳过 Daily 运行状态更新：当前文件属于更新的运行批次 "
                    f"{current_run_id}，本进程为 {self.run_id}。"
                )
                return False

            state.update(changes)
            state["updated_at"] = self._iso_now()
            self._write_unlocked(state)
            return True

    def record_auth(self, mode: str, results: Mapping[str, bool]) -> bool:
        return self.update(
            auth_mode=mode,
            auth_results={name: bool(succeeded) for name, succeeded in results.items()},
        )

    def mark_ledger_reset(self) -> bool:
        return self.update(ledger_reset=True)

    def finish(self) -> bool:
        return self.update(phase="finished")
