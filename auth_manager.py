#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily 登录预检编排：清理旧状态、分发登录流程并汇总结果。"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence

from playwright.async_api import BrowserContext

from login_flows import LoginRuntime, get_login_flow


logger = logging.getLogger("BackfillEngine")


@dataclass(frozen=True)
class AuthReport:
    """一次登录预检结果。"""

    results: Dict[str, bool]

    @property
    def all_succeeded(self) -> bool:
        return bool(self.results) and all(self.results.values())

    @property
    def any_succeeded(self) -> bool:
        return any(self.results.values())

    @property
    def succeeded_platforms(self) -> List[str]:
        return [name for name, succeeded in self.results.items() if succeeded]

    @property
    def failed_platforms(self) -> List[str]:
        return [name for name, succeeded in self.results.items() if not succeeded]

    @property
    def mode(self) -> str:
        if self.all_succeeded:
            return "NORMAL"
        if self.any_succeeded:
            return "DEGRADED"
        return "AUTH_REQUIRED"


class AuthManager:
    """在创建 datatoolcenter Worker 前编排各业务平台的登录预检。"""

    def __init__(self, bite_id: str, cookie_dir: Path):
        self.bite_id = bite_id
        self.cookie_dir = Path(cookie_dir)

    def _build_runtime(self, context: BrowserContext) -> LoginRuntime:
        return LoginRuntime(
            context=context,
            bite_id=self.bite_id,
            cookie_dir=self.cookie_dir,
        )

    async def ensure_platform(
        self,
        runtime: LoginRuntime,
        platform: Dict[str, Any],
    ) -> bool:
        """根据 auth_mode 将一个平台交给对应的具体登录流程。"""
        auth_mode = str(platform.get("auth_mode", "pkl_cookie"))
        login_flow = get_login_flow(auth_mode)
        if login_flow is None:
            logger.error(
                f"{platform['name']} 的 auth_mode={auth_mode!r} 未注册，"
                "该平台登录预检失败。"
            )
            return False
        return await login_flow(runtime, platform)

    async def ensure_platforms(
        self,
        context: BrowserContext,
        platforms: Sequence[Dict[str, Any]],
    ) -> AuthReport:
        """准备共享登录环境，再按配置顺序检查全部平台。"""
        runtime = self._build_runtime(context)
        results: Dict[str, bool] = {}
        for platform in platforms:
            platform_name = platform["name"]
            results[platform_name] = await self.ensure_platform(runtime, platform)
        return AuthReport(results=results)
