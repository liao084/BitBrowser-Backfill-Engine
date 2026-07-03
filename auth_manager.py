#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily-mode 登录预检：加载 pkl Cookie，并保留失败平台的诊断页面。"""

import logging
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError


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


class CookieAuthManager:
    """在创建 datatoolcenter Worker 前检查各真实工作平台登录状态。"""

    def __init__(self, bite_id: str, cookie_dir: Path):
        self.bite_id = bite_id
        self.cookie_dir = Path(cookie_dir)

    @staticmethod
    def _is_login_page(url: str, platform: Dict[str, Any]) -> bool:
        normalized_url = url.lower()
        markers = platform.get("login_url_markers", ["/login"])
        return any(str(marker).lower() in normalized_url for marker in markers)

    def _cookie_candidates(self, platform: Dict[str, Any]) -> List[Path]:
        file_prefix = platform.get("file_prefix")
        candidates = [
            self.cookie_dir / f"{self.bite_id}.pkl",
            self.cookie_dir / "cookies_latest.pkl",
        ]
        if file_prefix:
            candidates.extend(
                [
                    self.cookie_dir / f"{file_prefix}_{self.bite_id}.pkl",
                    self.cookie_dir / f"{file_prefix}_latest.pkl",
                ]
            )
        return candidates

    def _load_platform_cookies(
        self,
        platform: Dict[str, Any],
    ) -> Optional[List[Dict[str, Any]]]:
        platform_name = platform["name"]
        cookie_key = platform.get("cookie_key", platform_name)

        for cookie_file in self._cookie_candidates(platform):
            if not cookie_file.exists():
                continue
            try:
                with cookie_file.open("rb") as file_handle:
                    cookie_data = pickle.load(file_handle)
            except Exception as error:
                logger.warning(f"Cookie 文件无法读取 {cookie_file.name}: {error}")
                continue

            if isinstance(cookie_data, dict):
                cookies = cookie_data.get(cookie_key)
            elif isinstance(cookie_data, list):
                cookies = cookie_data
            else:
                cookies = None

            if isinstance(cookies, list) and cookies:
                logger.info(
                    f"✓ 已为 {platform_name} 加载 Cookie 文件 "
                    f"{cookie_file.name}（{len(cookies)} 条）。"
                )
                return cookies

            logger.warning(
                f"Cookie 文件 {cookie_file.name} 中没有 {cookie_key} 的有效数据。"
            )

        logger.error(
            f"未找到 {platform_name} 的有效 Cookie；已检查目录: {self.cookie_dir}"
        )
        return None

    @staticmethod
    def _format_cookie(cookie: Dict[str, Any], home_url: str) -> Optional[Dict[str, Any]]:
        if "name" not in cookie or "value" not in cookie:
            return None

        formatted: Dict[str, Any] = {
            "name": str(cookie["name"]),
            "value": str(cookie["value"]),
        }

        domain = cookie.get("domain")
        if domain:
            formatted["domain"] = str(domain)
            formatted["path"] = str(cookie.get("path", "/"))
        else:
            parsed = urlparse(home_url)
            formatted["url"] = f"{parsed.scheme}://{parsed.netloc}"

        if "secure" in cookie:
            formatted["secure"] = bool(cookie["secure"])
        if "httpOnly" in cookie:
            formatted["httpOnly"] = bool(cookie["httpOnly"])

        same_site = cookie.get("sameSite")
        same_site_map = {
            "strict": "Strict",
            "lax": "Lax",
            "none": "None",
            "no_restriction": "None",
        }
        if isinstance(same_site, str):
            normalized_same_site = same_site_map.get(same_site.lower())
            if normalized_same_site:
                formatted["sameSite"] = normalized_same_site

        expires = cookie.get("expires", cookie.get("expiry"))
        if isinstance(expires, (int, float)) and expires > 0:
            formatted["expires"] = float(expires)

        return formatted

    async def _apply_cookies(
        self,
        context: BrowserContext,
        cookies: Sequence[Dict[str, Any]],
        home_url: str,
    ) -> int:
        """逐条注入，避免一个损坏 Cookie 让整批有效 Cookie 一起失败。"""
        success_count = 0
        for raw_cookie in cookies:
            formatted_cookie = self._format_cookie(raw_cookie, home_url)
            if formatted_cookie is None:
                continue
            try:
                await context.add_cookies([formatted_cookie])
                success_count += 1
            except Exception as error:
                logger.debug(
                    f"跳过无法注入的 Cookie {raw_cookie.get('name', 'unknown')}: {error}"
                )
        return success_count

    async def _goto_home(self, page: Page, home_url: str) -> None:
        try:
            await page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightTimeoutError:
            logger.warning(f"访问平台主页等待超时，将根据当前 URL 继续判断: {home_url}")
        await page.wait_for_timeout(2000)

    async def ensure_platform(
        self,
        context: BrowserContext,
        platform: Dict[str, Any],
    ) -> bool:
        """检查一个平台；成功时关闭预检页，失败时保留页面供人工巡检。"""
        platform_name = platform["name"]
        home_url = platform["home_url"]
        page = await context.new_page()
        succeeded = False

        try:
            logger.info(f"开始检查 {platform_name} 登录状态: {home_url}")
            await self._goto_home(page, home_url)
            if not self._is_login_page(page.url, platform):
                logger.info(f"✓ {platform_name} 当前登录状态正常。")
                succeeded = True
                return True

            logger.warning(f"{platform_name} 已进入登录页面: {page.url}")
            if not platform.get("cookie_enabled", True):
                logger.warning(
                    f"{platform_name} 未启用 pkl Cookie 恢复；登录页将保留供人工处理。"
                )
                return False

            cookies = self._load_platform_cookies(platform)
            if not cookies:
                return False

            success_count = await self._apply_cookies(context, cookies, home_url)
            logger.info(
                f"已向浏览器上下文注入 {platform_name} Cookie: "
                f"{success_count}/{len(cookies)}"
            )
            if success_count == 0:
                return False

            await self._goto_home(page, home_url)
            if self._is_login_page(page.url, platform):
                logger.error(
                    f"{platform_name} 注入 Cookie 后仍处于登录页面，保留页面供人工处理。"
                )
                return False

            logger.info(f"✓ {platform_name} Cookie 恢复并验证成功。")
            succeeded = True
            return True
        except Exception as error:
            logger.error(
                f"{platform_name} 登录预检发生异常，保留当前页面供人工处理: {error}"
            )
            return False
        finally:
            if succeeded and not page.is_closed():
                try:
                    await page.close()
                except Exception as error:
                    logger.warning(f"关闭 {platform_name} 成功预检页失败: {error}")

    async def ensure_platforms(
        self,
        context: BrowserContext,
        platforms: Sequence[Dict[str, Any]],
    ) -> AuthReport:
        """顺序检查平台，避免多个登录页面并发跳转带来额外风控。"""
        results: Dict[str, bool] = {}
        for platform in platforms:
            platform_name = platform["name"]
            results[platform_name] = await self.ensure_platform(context, platform)
        return AuthReport(results=results)
