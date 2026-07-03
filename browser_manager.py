#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""比特浏览器生命周期管理：启动前重置并返回 CDP 调试地址。"""

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import requests


logger = logging.getLogger("BackfillEngine")


class BitBrowserManager:
    """通过本地比特浏览器 API 关闭、启动指定浏览器。"""

    def __init__(self, bite_id: str, bt_url: str = "http://127.0.0.1:54345"):
        self.bite_id = bite_id
        self.bt_url = bt_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}

    def _post(self, endpoint: str, timeout: int = 15) -> Dict[str, Any]:
        response = requests.post(
            f"{self.bt_url}{endpoint}",
            headers=self.headers,
            data=json.dumps({"id": self.bite_id}),
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    async def close_before_start(self, settle_seconds: float = 2.0) -> None:
        """尽力关闭旧实例；关闭失败不阻止后续 open 接口自行判断。"""
        logger.info(f"正在关闭可能遗留的比特浏览器 (ID: {self.bite_id})...")
        try:
            result = await asyncio.to_thread(self._post, "/browser/close")
            if result.get("success"):
                logger.info("✓ 旧比特浏览器已关闭。")
            else:
                logger.warning(
                    "比特浏览器关闭接口未返回成功，仍将继续尝试启动: "
                    f"{result.get('msg', result)}"
                )
        except Exception as error:
            logger.warning(f"关闭旧比特浏览器时发生异常，仍将继续尝试启动: {error}")

        await asyncio.sleep(settle_seconds)

    async def open_browser(
        self,
        max_attempts: int = 5,
        retry_delay_seconds: float = 2.0,
    ) -> Optional[str]:
        """启动浏览器并返回形如 127.0.0.1:xxxx 的 CDP 地址。"""
        for attempt in range(1, max_attempts + 1):
            try:
                result = await asyncio.to_thread(self._post, "/browser/open")
                data = result.get("data") or {}
                cdp_address = data.get("http") if isinstance(data, dict) else None
                if result.get("success") and cdp_address:
                    logger.info(f"✓ 比特浏览器启动成功，CDP 地址: {cdp_address}")
                    return cdp_address

                logger.warning(
                    f"第 {attempt}/{max_attempts} 次启动比特浏览器失败: "
                    f"{result.get('msg', result)}"
                )
            except Exception as error:
                logger.warning(
                    f"第 {attempt}/{max_attempts} 次启动比特浏览器发生异常: {error}"
                )

            if attempt < max_attempts:
                await asyncio.sleep(retry_delay_seconds)

        logger.error("多次尝试后仍无法启动比特浏览器。")
        return None
