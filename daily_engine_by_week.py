#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""固定采集上周一至上周日；复用 DailyEngine，仅适配任务日期和周组件注入。

沿用 GUI 生成的 daily .env，无需新增配置。周入口强制使用“周”维度，
忽略 TARGET_DATE、单任务 date 和所有日期偏移；这些字段仍经过原配置校验。
源码运行：python daily_engine_by_week.py。打包时使用本文件作为入口。
"""

import asyncio
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from playwright.async_api import Locator, Page

from daily_engine import DailyEngine, load_daily_runtime_config, logger
from github_info import GIT_SHA


INJECT_WEEK_DATE_JS = r"""async (input, dateText) => {
    const vm = input.closest('.el-date-editor')?.__vue__?.$parent;
    if (vm?.$options.name !== 'ElDatePicker' || vm.type !== 'week') {
        throw new Error('未找到当前输入框对应的 ElDatePicker 周组件');
    }
    if (vm.pickerDisabled || vm.readonly) {
        throw new Error('周日期组件当前不可修改');
    }
    for (const name of ['isValidValue', 'emitInput', 'emitChange', '$nextTick']) {
        if (typeof vm[name] !== 'function') {
            throw new Error(`周日期组件缺少方法：${name}`);
        }
    }

    const [year, month, day] = dateText.split('-').map(Number);
    const target = new Date(year, month - 1, day);
    if (Number.isNaN(target.getTime()) || target.getFullYear() !== year ||
        target.getMonth() !== month - 1 || target.getDate() !== day) {
        throw new Error(`目标日期不存在：${dateText}`);
    }
    if (!vm.isValidValue(target)) {
        throw new Error(`目标日期不符合组件可选规则：${dateText}`);
    }

    // 周组件可能回传同周的代表日期；按周一归属校验，不自算显示周编号。
    const mondayTime = value => {
        const monday = new Date(value.getFullYear(), value.getMonth(), value.getDate());
        monday.setDate(monday.getDate() - (monday.getDay() + 6) % 7);
        return monday.getTime();
    };
    const checkValue = () => {
        const value = vm.parsedValue;
        if (!(value instanceof Date) || Number.isNaN(value.getTime()) ||
            mondayTime(value) !== mondayTime(target)) {
            throw new Error(`周日期绑定未落在目标周内：${dateText}`);
        }
        const display = vm.displayValue;
        if (typeof display !== 'string' || !/^\d{4}\s*第\s*\d{1,2}\s*周$/.test(display.trim())) {
            throw new Error(`周日期显示异常：${String(display)}`);
        }
        return {
            boundDate: [value.getFullYear(), value.getMonth() + 1, value.getDate()],
            displayValue: display
        };
    };

    vm.userInput = null;
    vm.emitInput(target);
    await vm.$nextTick();
    checkValue();
    vm.emitChange(vm.value);
    await vm.$nextTick();
    return checkValue();
}"""


def previous_week_range(today: date) -> tuple[str, str]:
    """返回 today 所在自然周之前一周的周一、周日（YYYY-MM-DD）。"""
    this_monday = today - timedelta(days=today.weekday())
    return (
        (this_monday - timedelta(days=7)).isoformat(),
        (this_monday - timedelta(days=1)).isoformat(),
    )


class WeeklyEngine(DailyEngine):
    """复用日常采集编排，每张卡片只生成一个上周任务。"""

    def __init__(
        self,
        bite_id: str,
        gc_page_url_markers: Sequence[str],
        **kwargs: Any,
    ):
        kwargs["time_type"] = "周"
        super().__init__(bite_id, gc_page_url_markers, **kwargs)

    def build_daily_tasks(
        self,
        tasks_config: Sequence[Dict[str, Any]],
        target_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """忽略日模式日期设置，复用父类的任务结构和卡片去重。"""
        start_date, end_date = previous_week_range(date.today())
        weekly_configs = [
            {"card_id": config["card_id"], "date": start_date}
            for config in tasks_config
        ]
        tasks = super().build_daily_tasks(weekly_configs)
        for task in tasks:
            task["end"] = end_date
            task["task_id"] = f"card-{task['card']}_week_{start_date}_{end_date}"
        logger.info(
            "周任务池：%s 至 %s，共 %s 张唯一卡片；忽略日模式指定日期及偏移。",
            start_date,
            end_date,
            len(tasks),
        )
        return tasks

    async def _inject_week_date(
        self,
        input_locator: Locator,
        target_date: str,
        worker_id: str,
        field_name: str,
    ) -> Dict[str, Any]:
        """调用已验证的组件事件链，并复用父类的页面操作硬超时。"""
        # 保留周一/周日边界：两端可选范围按实际日期联动，不能都改成周一。
        normalized_date = date.fromisoformat(target_date).isoformat()
        await input_locator.wait_for(state="visible", timeout=30000)
        result = await self._await_page_operation(
            input_locator.evaluate(INJECT_WEEK_DATE_JS, normalized_date),
            worker_id,
            f"注入{field_name}周日期 {target_date}",
        )
        logger.info(
            "Worker-%s %s日期 %s → %s，组件绑定日期=%s",
            worker_id,
            field_name,
            normalized_date,
            result["displayValue"],
            result["boundDate"],
        )
        return result

    async def _initialize_week_range(
        self,
        start_input: Locator,
        end_input: Locator,
        worker_id: str,
    ) -> None:
        """以最晚可用完整周初始化区间：先放宽结束边界，再设置开始边界。

        初始化日期独立于采集目标；调用方随后仍须完整设置目标开始、结束日期。
        即使初始化跨周发生变化，最终目标仍使用任务中已经确定的区间。
        """
        init_start, init_end = previous_week_range(date.today())
        logger.info(
            "Worker-%s 初始化周区间：%s 至 %s，先结束、后开始",
            worker_id,
            init_start,
            init_end,
        )
        await self._inject_week_date(end_input, init_end, worker_id, "结束")
        await self._inject_week_date(start_input, init_start, worker_id, "开始")

    async def inject_dates(
        self,
        page: Page,
        start_date: str,
        end_date: str,
        worker_id: str,
    ) -> None:
        """统一执行区间初始化和目标注入；目标由调用方提供，与采集模式无关。"""
        logger.info(
            "Worker-%s 开始注入周采集区间：%s 至 %s",
            worker_id,
            start_date,
            end_date,
        )
        try:
            monday = date.fromisoformat(start_date)
            sunday = date.fromisoformat(end_date)
            if monday.weekday() != 0 or sunday.weekday() != 6 or monday > sunday:
                raise ValueError("目标周区间必须从周一开始、周日结束，且开始不晚于结束")

            primary_drawer = self._primary_drawer(page)
            start_input = primary_drawer.get_by_role("textbox", name="开始", exact=False)
            end_input = primary_drawer.get_by_role("textbox", name="结束", exact=False)
            for field_name, input_locator in (("开始", start_input), ("结束", end_input)):
                count = await self._await_page_operation(
                    input_locator.count(), worker_id, f"查询{field_name}日期输入框数量"
                )
                if count != 1:
                    raise RuntimeError(f"预期找到 1 个{field_name}日期输入框，实际 {count} 个")

            await self._initialize_week_range(start_input, end_input, worker_id)

            logger.info("Worker-%s 初始化完成，按先开始、后结束设置目标区间", worker_id)
            await self._inject_week_date(
                start_input, start_date, worker_id, "开始"
            )
            await self._inject_week_date(
                end_input, end_date, worker_id, "结束"
            )
        except Exception:
            logger.exception("Worker-%s 周日期注入失败", worker_id)
            raise


def main() -> int:
    logger.info("程序版本: app=daily_engine_by_week, git_sha=%s", GIT_SHA)
    try:
        config = load_daily_runtime_config()
    except (OSError, ValueError) as error:
        logger.error("周采集配置加载失败: %s", error)
        return 1

    engine = WeeklyEngine(
        bite_id=config.bite_id,
        gc_page_url_markers=config.gc_page_url_markers,
        worker_count=config.worker_count,
        max_attempts=config.max_attempts,
        cookie_dir=config.cookie_dir,
        task_url=config.task_url,
        keep_browser_after_run=config.keep_browser_after_run,
        worker_heartbeat_silence_seconds=config.worker_heartbeat_silence_seconds,
        business_heartbeat_silence_seconds=config.business_heartbeat_silence_seconds,
    )
    succeeded = asyncio.run(engine.run_daily(config.daily_tasks, config.platforms))
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
