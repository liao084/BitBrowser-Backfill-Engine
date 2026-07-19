#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过本机 Edge CDP 连接执行历史补采。"""

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# backfill_engine 在导入时初始化日志，因此必须先指定 Edge 独立日志名。
os.environ.setdefault("RPA_LOG_FILENAME", "edgebackfill_run.log")

import requests
from dotenv import load_dotenv

from backfill_engine import (
    BackfillEngine,
    _load_json_list_env,
    logger,
    runtime_dir,
)


def _normalize_cdp_address(raw_address: str) -> str:
    """将 host:port 或 http://host:port 统一为 Playwright 使用的 host:port。"""
    value = raw_address.strip()
    if not value:
        raise ValueError("EDGE_CDP_ADDRESS 不能为空")

    candidate = value if "://" in value else f"http://{value}"
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"EDGE_CDP_ADDRESS 端口无效: {value}") from error

    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "EDGE_CDP_ADDRESS 格式应为 host:port，例如 127.0.0.1:9222"
        )

    return parsed.netloc


class EdgeBackfillEngine(BackfillEngine):
    """复用历史补采流程，仅将浏览器来源替换为现有 Edge CDP。"""

    def __init__(
        self,
        edge_cdp_address: str,
        gc_page_url_markers: List[str],
        worker_silent_timeout_seconds: int = 200,
        gc_silent_timeout_seconds: int = 240,
    ):
        super().__init__(
            bite_id="edge-cdp",
            gc_page_url_markers=gc_page_url_markers,
        )
        self.edge_cdp_address = _normalize_cdp_address(edge_cdp_address)
        if worker_silent_timeout_seconds <= 0:
            raise ValueError("worker_silent_timeout_seconds 必须大于 0")
        if gc_silent_timeout_seconds <= worker_silent_timeout_seconds:
            raise ValueError(
                "gc_silent_timeout_seconds 必须大于 worker_silent_timeout_seconds"
            )

        self.silent_timeout_seconds = worker_silent_timeout_seconds
        self.gc_silent_timeout_seconds = gc_silent_timeout_seconds
        self.gc_shutdown_grace_seconds = (
            self.gc_silent_timeout_seconds - self.silent_timeout_seconds + 5
        )

    def get_debugger_address(self) -> Optional[str]:
        """确认 Edge CDP 已开启，并返回可交给 Playwright 的调试地址。"""
        version_url = f"http://{self.edge_cdp_address}/json/version"
        logger.info(f"正在检查 Edge CDP: {version_url}")

        try:
            response = requests.get(version_url, timeout=10)
            response.raise_for_status()
            version_info = response.json()
            if not isinstance(version_info, dict):
                logger.error("Edge CDP /json/version 返回的不是 JSON 对象")
                return None
            websocket_url = version_info.get("webSocketDebuggerUrl")
            if not websocket_url:
                logger.error("Edge CDP 响应缺少 webSocketDebuggerUrl")
                return None

            browser_name = version_info.get("Browser", "未知浏览器")
            if not str(browser_name).startswith("Edg/"):
                logger.warning(f"CDP 返回的浏览器不是 Edge: {browser_name}")

            logger.info(
                f"✓ Edge CDP 连接检查成功: {self.edge_cdp_address} ({browser_name})"
            )
            return self.edge_cdp_address
        except (requests.RequestException, ValueError) as error:
            logger.error(
                "✗ 无法连接 Edge CDP；请确认 Edge 已使用 "
                f"--remote-debugging-port 启动: {error}"
            )
            return None


def load_runtime_config() -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """从源码或 exe 同目录的 .env 加载 Edge 历史补采配置。"""
    env_path = runtime_dir / ".env"
    if not env_path.exists():
        raise FileNotFoundError(
            f"未找到运行配置 {env_path}；请复制 .env.example 为 .env 后填写。"
        )

    load_dotenv(env_path)
    edge_cdp_address = (
        os.getenv("EDGE_CDP_ADDRESS") or "127.0.0.1:9222"
    ).strip()
    edge_cdp_address = _normalize_cdp_address(edge_cdp_address)

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

    return edge_cdp_address, tasks_config_raw, markers_raw


if __name__ == "__main__":
    try:
        cdp_address, tasks_config, gc_page_url_markers = load_runtime_config()
    except (OSError, ValueError) as error:
        logger.error(f"运行配置加载失败: {error}")
        sys.exit(1)

    engine = EdgeBackfillEngine(
        cdp_address,
        gc_page_url_markers=gc_page_url_markers,
    )
    asyncio.run(engine.run(tasks_config))
