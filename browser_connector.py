#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史补采使用的浏览器 CDP 地址获取器。"""

import logging
from typing import Optional, Protocol
from urllib.parse import urlsplit

import requests


logger = logging.getLogger("BackfillEngine")


class BrowserConnector(Protocol):
    """统一约定：返回可交给 Playwright 的 host:port CDP 地址。"""

    def get_cdp_address(self) -> Optional[str]: ...


def normalize_cdp_address(raw_address: str) -> str:
    """将 host:port 或 http://host:port 统一为 host:port。"""
    value = raw_address.strip()
    if not value:
        raise ValueError("CDP_ADDRESS 不能为空")

    candidate = value if "://" in value else f"http://{value}"
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"CDP_ADDRESS 端口无效: {value}") from error

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
            "CDP_ADDRESS 格式应为 host:port，例如 127.0.0.1:9222"
        )

    return parsed.netloc


class BitBrowserConnector:
    """通过比特浏览器本地 API 启动指定浏览器并取得 CDP 地址。"""

    def __init__(
        self,
        bite_id: str,
        api_base_url: str = "http://127.0.0.1:54345",
    ):
        self.bite_id = bite_id
        self.api_base_url = api_base_url

    def get_cdp_address(self) -> Optional[str]:
        logger.info(f"正在尝试连接比特浏览器 (ID: {self.bite_id})...")
        url = f"{self.api_base_url}/browser/open"
        try:
            response = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json={"id": self.bite_id},
                timeout=15,
            )
            if response.status_code != 200:
                logger.error(f"✗ API 请求失败 HTTP {response.status_code}")
                return None

            result = response.json()
            if not result.get("success") or "data" not in result:
                logger.error(f"✗ 浏览器启动响应错误: {result.get('msg')}")
                return None

            cdp_address = result["data"].get("http")
            if not cdp_address:
                logger.error("✗ 浏览器启动响应缺少 CDP 调试地址")
                return None

            normalized_address = normalize_cdp_address(str(cdp_address))
            logger.info(
                f"✓ 获取浏览器 CDP 调试地址成功: {normalized_address}"
            )
            return normalized_address
        except (requests.exceptions.RequestException, ValueError) as error:
            logger.error(f"✗ 浏览器连接异常: {error}")
            return None


class ExternalCdpConnector:
    """检查一个已由用户开启远程调试的 Chromium 浏览器。"""

    def __init__(self, cdp_address: str):
        self.cdp_address = normalize_cdp_address(cdp_address)

    def get_cdp_address(self) -> Optional[str]:
        version_url = f"http://{self.cdp_address}/json/version"
        logger.info(f"正在检查外部浏览器 CDP: {version_url}")

        try:
            response = requests.get(version_url, timeout=10)
            response.raise_for_status()
            version_info = response.json()
            if not isinstance(version_info, dict):
                logger.error("CDP /json/version 返回的不是 JSON 对象")
                return None
            if not version_info.get("webSocketDebuggerUrl"):
                logger.error("CDP 响应缺少 webSocketDebuggerUrl")
                return None

            browser_name = version_info.get("Browser", "未知浏览器")
            logger.info(
                f"✓ 外部浏览器 CDP 检查成功: "
                f"{self.cdp_address} ({browser_name})"
            )
            return self.cdp_address
        except (requests.exceptions.RequestException, ValueError) as error:
            logger.error(
                "✗ 无法连接外部浏览器 CDP；请确认浏览器已使用 "
                f"--remote-debugging-port 启动: {error}"
            )
            return None
