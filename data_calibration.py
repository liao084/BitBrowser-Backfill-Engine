#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可按需挂载的数据校准流程。"""

import logging
from datetime import date, timedelta

from playwright.async_api import BrowserContext, expect


CALIBRATION_URL = "https://datatoolcenter.com/web/testone1.html"
PAGE_READY_SELECTOR = "span.el-pagination__total"
SUCCESS_TOAST_SELECTOR = ".el-message__content:has-text('同步完成')"
RESTORATION_SUCCESS_SELECTOR = ".el-message__content:has-text('修复完成')"
ACTION_TIMEOUT_MS = 30_000
COMPLETION_TIMEOUT_MS = 600_000


def build_calibration_url() -> str:
    """使用本机昨天日期生成当前固定数据板块的校准地址。"""
    target_date = (date.today() - timedelta(days=1)).isoformat()
    return (
        f"{CALIBRATION_URL}?"
        "activeName=jdbrand_industry_productList&"
        "menuplat=%E4%BA%AC%E4%B8%9C&"
        "currentMenuIndex=642&"
        f"start={target_date}&end={target_date}&"
        "dateType=day&"
        "runAsUserId=%E5%85%A8%E9%83%A8%E5%BA%97%E9%93%BA"
    )


async def run_data_calibration(
    context: BrowserContext,
    *,
    logger: logging.Logger,
    action_timeout_ms: int = ACTION_TIMEOUT_MS,
    completion_timeout_ms: int = COMPLETION_TIMEOUT_MS,
) -> None:
    """在独立标签页执行昨天数据的数据修复和京东校准流程。"""
    calibration_url = build_calibration_url()
    page = await context.new_page()
    logger.info(f"开始执行数据校准: {calibration_url}")

    await page.goto(
        calibration_url,
        wait_until="domcontentloaded",
        timeout=action_timeout_ms,
    )

    pagination_total = page.locator(PAGE_READY_SELECTOR)
    await expect(pagination_total).to_be_visible(timeout=action_timeout_ms)
    logger.info("数据校准页面已完成稳定渲染。")

    # 由于偶发因素会导致本应自动触发的数据修复功能未能正常生效，所以显式的执行数据修复功能
    restoration_button = page.get_by_role("button",name="数据修复",exact=False)
    await restoration_button.click(timeout=action_timeout_ms)
    logger.info("数据修复已触发，正在等待修复完成信号...")

    # 点击数据修复按钮之后,等待修复完成弹窗出现
    restoration_success_toast = page.locator(RESTORATION_SUCCESS_SELECTOR)
    await restoration_success_toast.wait_for(
        state="visible",
        timeout=completion_timeout_ms
    )
    logger.info("✓ 数据修复完成：已捕获修复完成信号。")


    settings_button = page.get_by_role(
        "button",
        name="表格设置",
        exact=False,
    )
    await settings_button.click(timeout=action_timeout_ms)

    menu_id = (await settings_button.get_attribute("aria-controls") or "").strip()
    if not menu_id:
        raise RuntimeError("表格设置按钮缺少 aria-controls，无法定位下拉菜单")

    dropdown_menu = page.locator(f'[id="{menu_id}"]')
    await expect(dropdown_menu).to_be_visible(timeout=action_timeout_ms)

    calibration_control = dropdown_menu.get_by_text(
        "京东校准",
        exact=False,
    )
    await calibration_control.click(timeout=action_timeout_ms)
    logger.info("京东校准已触发，等待同步完成信号...")

    success_toast = page.locator(SUCCESS_TOAST_SELECTOR)
    await success_toast.wait_for(
        state="visible",
        timeout=completion_timeout_ms,
    )
    logger.info("✓ 数据校准完成：已捕获同步完成信号。")
