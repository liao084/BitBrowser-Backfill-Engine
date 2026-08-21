#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily 登录流程：保存各认证模式完整、可独立阅读的页面操作。"""

from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)


logger = logging.getLogger("BackfillEngine")


@dataclass(frozen=True)
class LoginRuntime:
    """具体登录流程运行时需要的外部资源。"""

    context: BrowserContext
    bite_id: str
    cookie_dir: Path
    cleared_cookie_domains: set[str] = field(default_factory=set)


LoginFlow = Callable[[LoginRuntime, Dict[str, Any]], Awaitable[bool]]


def _is_login_page(url: str, platform: Dict[str, Any]) -> bool:
    normalized_url = url.lower()
    markers = platform.get("login_url_markers", ["/login"])
    return any(str(marker).lower() in normalized_url for marker in markers)


def _cookie_candidates(
    runtime: LoginRuntime,
    platform: Dict[str, Any],
) -> List[Path]:
    file_prefix = platform.get("file_prefix")
    candidates = [
        runtime.cookie_dir / f"{runtime.bite_id}.pkl",
        runtime.cookie_dir / "cookies_latest.pkl",
    ]
    if file_prefix:
        candidates.extend(
            [
                runtime.cookie_dir / f"{file_prefix}_{runtime.bite_id}.pkl",
                runtime.cookie_dir / f"{file_prefix}_latest.pkl",
            ]
        )
    return candidates


def _load_platform_cookies(
    runtime: LoginRuntime,
    platform: Dict[str, Any],
) -> Optional[List[Dict[str, Any]]]:
    platform_name = platform["name"]
    cookie_key = platform.get("cookie_key", platform_name)

    for cookie_file in _cookie_candidates(runtime, platform):
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
        f"未找到 {platform_name} 的有效 Cookie；已检查目录: {runtime.cookie_dir}"
    )
    return None


def _format_cookie(
    cookie: Dict[str, Any],
    home_url: str,
) -> Optional[Dict[str, Any]]:
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
    context: BrowserContext,
    cookies: Sequence[Dict[str, Any]],
) -> int:
    """逐条注入，避免一个损坏 Cookie 让整批有效 Cookie 一起失败。"""
    success_count = 0
    for cookie in cookies:
        try:
            await context.add_cookies([cookie])
            success_count += 1
        except Exception as error:
            logger.debug(
                f"跳过无法注入的 Cookie {cookie.get('name', 'unknown')}: {error}"
            )
    return success_count


def _prepare_cookies(
    cookies: Sequence[Dict[str, Any]],
    home_url: str,
) -> List[Dict[str, Any]]:
    """在清理旧 Cookie 前完成格式转换，避免无有效数据时破坏现有状态。"""
    prepared: List[Dict[str, Any]] = []
    for raw_cookie in cookies:
        formatted_cookie = _format_cookie(raw_cookie, home_url)
        if formatted_cookie is not None:
            prepared.append(formatted_cookie)
    return prepared


def _cookie_domains(
    cookies: Sequence[Dict[str, Any]],
) -> set[str]:
    """提取注入 Cookie 涉及的精确 domain；URL 型 Cookie 使用其主机名。"""
    domains: set[str] = set()
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).strip()
        if domain:
            domains.add(domain)
            continue

        cookie_url = str(cookie.get("url", "")).strip()
        if cookie_url:
            hostname = urlparse(cookie_url).hostname
            if hostname:
                domains.add(hostname)
    return domains


async def _clear_cookie_domains_once(
    runtime: LoginRuntime,
    platform_name: str,
    domains: set[str],
) -> None:
    """同一次登录预检中，每个精确 domain 最多清理一次。"""
    for domain in sorted(domains):
        if domain in runtime.cleared_cookie_domains:
            logger.info(
                f"{platform_name} Cookie domain 本轮已经清理，跳过重复操作: "
                f"{domain}"
            )
            continue

        logger.info(f"正在清理 {platform_name} 旧 Cookie domain: {domain}")
        await runtime.context.clear_cookies(domain=domain)
        runtime.cleared_cookie_domains.add(domain)


async def _goto_home(page: Page, home_url: str) -> None:
    try:
        await page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        logger.warning(f"访问平台主页等待超时，将根据当前 URL 继续判断: {home_url}")
    await page.wait_for_timeout(2000)


async def login_with_pkl_cookie(
    runtime: LoginRuntime,
    platform: Dict[str, Any],
) -> bool:
    """通过 pkl Cookie 重建并验证一个平台的登录态。"""
    platform_name = platform["name"]
    home_url = platform["home_url"]
    page = await runtime.context.new_page()
    succeeded = False

    try:
        logger.info(f"开始重建 {platform_name} 登录态: {home_url}")
        await _goto_home(page, home_url)
        initial_url = page.url
        is_login_page = _is_login_page(initial_url, platform)

        if not is_login_page and not platform.get("cookie_enabled", True):
            logger.info(
                f"✓ {platform_name} 当前登录状态正常，最终 URL: {initial_url}"
            )
            succeeded = True
            return True

        if is_login_page:
            logger.info(f"{platform_name} 已进入登录页面，准备注入 pkl Cookie: {initial_url}")
        else:
            logger.info(
                f"{platform_name} 当前未进入登录页，仍将按 pkl Cookie "
                f"重建登录态，当前 URL: {initial_url}"
            )

        if not platform.get("cookie_enabled", True):
            logger.warning(
                f"{platform_name} 未启用 pkl Cookie 恢复；登录页将保留供人工处理。"
            )
            return False

        cookies = _load_platform_cookies(runtime, platform)
        if not cookies:
            return False

        prepared_cookies = _prepare_cookies(cookies, home_url)
        if not prepared_cookies:
            logger.error(f"{platform_name} pkl 中没有可注入的有效 Cookie。")
            return False
        if len(prepared_cookies) != len(cookies):
            logger.warning(
                f"{platform_name} pkl 中有 "
                f"{len(cookies) - len(prepared_cookies)} 条 Cookie 缺少必要字段，"
                "已在清理旧状态前跳过。"
            )

        cookie_domains = _cookie_domains(prepared_cookies)
        if not cookie_domains:
            logger.error(f"{platform_name} pkl 中无法确定任何 Cookie domain。")
            return False

        await _clear_cookie_domains_once(
            runtime,
            platform_name,
            cookie_domains,
        )

        success_count = await _apply_cookies(
            runtime.context,
            prepared_cookies,
        )
        logger.info(
            f"已向浏览器上下文注入 {platform_name} Cookie: "
            f"{success_count}/{len(prepared_cookies)}"
        )
        if success_count == 0:
            return False

        await _goto_home(page, home_url)
        if _is_login_page(page.url, platform):
            logger.error(
                f"{platform_name} 注入 Cookie 后仍处于登录页面，"
                f"最终 URL: {page.url}；保留页面供人工处理。"
            )
            return False

        logger.info(f"✓ {platform_name} Cookie 重建并验证成功，最终 URL: {page.url}")
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


async def login_1688(
    runtime: LoginRuntime,
    platform: Dict[str, Any],
) -> bool:
    """沿用现有策略：有登录按钮就点击，没有则视为已登录。"""
    platform_name = platform["name"]
    home_url = platform["home_url"]
    page = await runtime.context.new_page()
    succeeded = False

    try:
        logger.info(f"开始检查 {platform_name} 登录状态: {home_url}")
        await _goto_home(page, home_url)
        login_button = page.locator("xpath=//*[@id='login-form']/div[6]/button")

        try:
            await login_button.wait_for(state="visible", timeout=10000)
        except PlaywrightTimeoutError:
            logger.info(
                f"✓ {platform_name} 未出现登录按钮，当前登录状态正常，"
                f"最终 URL: {page.url}"
            )
            succeeded = True
            return True

        logger.info(f"{platform_name} 检测到登录按钮，正在点击登录。")
        await login_button.click(timeout=10000)
        await page.wait_for_timeout(4000)

        success_locator = page.locator(
            "xpath=//*[@id='app']/div/div[1]/div/div/div/div[1]/ul/li[10]/a/span"
        )
        await success_locator.wait_for(state="visible", timeout=10000)
        logger.info(f"✓ {platform_name} 按钮登录成功，最终 URL: {page.url}")
        succeeded = True
        return True
    except Exception as error:
        logger.error(f"{platform_name} 按钮登录失败，保留当前页面供人工处理: {error}")
        return False
    finally:
        if succeeded and not page.is_closed():
            try:
                await page.close()
            except Exception as error:
                logger.warning(f"关闭 {platform_name} 成功预检页失败: {error}")
LOGIN_FLOW_REGISTRY: Dict[str, LoginFlow] = {
    "pkl_cookie": login_with_pkl_cookie,
    "1688_button_login": login_1688,
}


def get_login_flow(auth_mode: str) -> Optional[LoginFlow]:
    """返回认证模式对应的具体流程。"""
    return LOGIN_FLOW_REGISTRY.get(auth_mode)
