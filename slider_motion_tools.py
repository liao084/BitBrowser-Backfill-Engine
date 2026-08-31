#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""滑块图片距离计算与鼠标轨迹生成工具。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import math
import random
from functools import lru_cache
from io import BytesIO
from typing import Any, NamedTuple

from PIL import Image, UnidentifiedImageError
from playwright.async_api import Page


RenderedSize = tuple[float, float]
_SAMPLE_INTERVAL_SECONDS = 0.0167
_MIN_RANDOM_POINT_COUNT = 49
_MAX_RANDOM_POINT_COUNT = 74


class TrajectoryPoint(NamedTuple):
    """相对于鼠标按下点的一个轨迹采样点。"""

    elapsed_seconds: float
    x: float
    y: float


class _SliderSnapshot(NamedTuple):
    """从 closed Shadow DOM 中读取的一次滑块页面快照。"""

    target_data_url: str
    background_data_url: str
    target_rendered_size: RenderedSize
    background_rendered_size: RenderedSize
    button_center: tuple[float, float]


def _decode_image_data_url(data_url: str, image_name: str) -> bytes:
    """将 Base64 图片 Data URL 解码为图片文件字节。"""
    if not isinstance(data_url, str):
        raise TypeError(f"{image_name} data URL 必须是字符串")

    try:
        header, payload = data_url.split(",", 1)
    except ValueError as error:
        raise ValueError(f"{image_name} 不是有效的 Data URL") from error

    if not header.startswith("data:image/") or ";base64" not in header:
        raise ValueError(f"{image_name} 必须是 Base64 图片 Data URL")

    try:
        return base64.b64decode("".join(payload.split()), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"{image_name} Base64 数据无效") from error


def _get_image_size(image_bytes: bytes, image_name: str) -> tuple[int, int]:
    """读取并验证图片的原始像素尺寸。"""
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
            width, height = image.size
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError(f"{image_name} 不是可识别的图片") from error

    if width <= 0 or height <= 0:
        raise ValueError(f"{image_name} 原始尺寸无效: {width}x{height}")
    return width, height


def _validate_rendered_size(
    rendered_size: RenderedSize,
    image_name: str,
) -> tuple[float, float]:
    """验证浏览器中的图片渲染尺寸。"""
    if len(rendered_size) != 2:
        raise ValueError(f"{image_name} 渲染尺寸必须是 (width, height)")

    width, height = map(float, rendered_size)
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError(
            f"{image_name} 渲染尺寸必须是正的有限数值: {rendered_size}"
        )
    return width, height


def _validate_uniform_scale(
    original_size: tuple[int, int],
    rendered_size: tuple[float, float],
    image_name: str,
) -> tuple[float, float]:
    """计算横纵缩放比例，并确认图片保持等比例缩放。"""
    original_width, original_height = original_size
    rendered_width, rendered_height = rendered_size
    scale_x = rendered_width / original_width
    scale_y = rendered_height / original_height

    if not math.isclose(scale_x, scale_y, rel_tol=0.02, abs_tol=1e-6):
        raise ValueError(
            f"{image_name} 未保持等比例缩放: scale_x={scale_x:.6f}, "
            f"scale_y={scale_y:.6f}"
        )
    return scale_x, scale_y


@lru_cache(maxsize=1)
def _get_slider_matcher():
    """复用无需 OCR 模型的 ddddocr 滑块匹配器。"""
    import ddddocr

    return ddddocr.DdddOcr(ocr=False, det=False, show_ad=False)


def calculate_slider_drag_distance(
    target_data_url: str,
    background_data_url: str,
    target_rendered_size: RenderedSize,
    background_rendered_size: RenderedSize,
    *,
    simple_target: bool = False,
) -> float:
    """计算 Playwright 鼠标需要水平移动的 CSS 像素距离。

    本函数采用 ddddocr 1.6.1 的中心点返回协议。它假定：

    - target 与 background 的初始左边缘处于同一水平坐标原点；
    - target 的中心点与页面滑块按钮的中心点一致；
    - 两张图片都在浏览器中完整、等比例缩放，没有裁剪。

    Args:
        target_data_url: target 图片的 Base64 Data URL。
        background_data_url: background 图片的 Base64 Data URL。
        target_rendered_size: target 在浏览器中的 ``(width, height)``。
        background_rendered_size: background 在浏览器中的
            ``(width, height)``。
        simple_target: 直接传递给 ddddocr ``slide_match``。

    Returns:
        鼠标需要水平移动的 CSS 像素距离。正值表示向右。
    """
    target_bytes = _decode_image_data_url(target_data_url, "target")
    background_bytes = _decode_image_data_url(
        background_data_url,
        "background",
    )

    target_original_size = _get_image_size(target_bytes, "target")
    background_original_size = _get_image_size(
        background_bytes,
        "background",
    )
    target_rendered_size = _validate_rendered_size(
        target_rendered_size,
        "target",
    )
    background_rendered_size = _validate_rendered_size(
        background_rendered_size,
        "background",
    )

    target_scale_x, _ = _validate_uniform_scale(
        target_original_size,
        target_rendered_size,
        "target",
    )
    background_scale_x, _ = _validate_uniform_scale(
        background_original_size,
        background_rendered_size,
        "background",
    )

    match_result = _get_slider_matcher().slide_match(
        target_bytes,
        background_bytes,
        simple_target=simple_target,
    )
    try:
        matched_center_x = float(match_result["target_x"])
        matched_center_y = float(match_result["target_y"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "ddddocr 返回值缺少有效的 target_x/target_y 中心点"
        ) from error

    background_width, background_height = background_original_size
    if not 0 <= matched_center_x <= background_width:
        raise ValueError(f"ddddocr target_x 超出背景图片范围: {matched_center_x}")
    if not 0 <= matched_center_y <= background_height:
        raise ValueError(f"ddddocr target_y 超出背景图片范围: {matched_center_y}")

    target_center_source_x = target_original_size[0] / 2
    target_center_css_x = target_center_source_x * target_scale_x
    matched_center_css_x = matched_center_x * background_scale_x
    return matched_center_css_x - target_center_css_x


async def calculate_slider_drag_distance_async(
    target_data_url: str,
    background_data_url: str,
    target_rendered_size: RenderedSize,
    background_rendered_size: RenderedSize,
    *,
    simple_target: bool = False,
) -> float:
    """在线程池中计算拖动距离，避免阻塞 Playwright 事件循环。"""
    return await asyncio.to_thread(
        calculate_slider_drag_distance,
        target_data_url,
        background_data_url,
        target_rendered_size,
        background_rendered_size,
        simple_target=simple_target,
    )


def generate_drag_trajectory(
    distance_x: float,
    *,
    duration_seconds: float | None = None,
    vertical_amplitude: float = 6.0,
    point_count: int | None = None,
    random_seed: int | str | bytes | bytearray | None = None,
) -> list[TrajectoryPoint]:
    """生成 Minimum Jerk 时间进度下的三次贝塞尔拖动轨迹。

    返回点使用相对于鼠标按下位置的局部 CSS 坐标。调用方应在相邻
    ``elapsed_seconds`` 之间等待相应时间，再将 ``x/y`` 加到鼠标按下点。

    默认随机生成 49～74 个采样点，并按每段约 16.7ms 推导总耗时。
    如果只传入总耗时，则按相同采样间隔反推采样点数量。
    """
    distance_x = float(distance_x)
    vertical_amplitude = float(vertical_amplitude)

    if not math.isfinite(distance_x):
        raise ValueError("distance_x 必须是有限数值")
    if not math.isfinite(vertical_amplitude) or vertical_amplitude < 0:
        raise ValueError("vertical_amplitude 必须是非负有限数值")

    if duration_seconds is not None:
        duration_seconds = float(duration_seconds)
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("duration_seconds 必须是正的有限数值")

    if point_count is not None:
        if isinstance(point_count, bool) or not isinstance(point_count, int):
            raise TypeError("point_count 必须是整数")
        if point_count < 2:
            raise ValueError("point_count 至少为 2")

    random_source = random.Random(random_seed)
    if point_count is None:
        if duration_seconds is None:
            point_count = random_source.randint(
                _MIN_RANDOM_POINT_COUNT,
                _MAX_RANDOM_POINT_COUNT,
            )
        else:
            point_count = max(
                2,
                round(duration_seconds / _SAMPLE_INTERVAL_SECONDS) + 1,
            )

    if duration_seconds is None:
        duration_seconds = round(
            (point_count - 1) * _SAMPLE_INTERVAL_SECONDS,
            1,
        )

    control_1_x = distance_x * random_source.uniform(0.2, 0.4)
    control_2_x = distance_x * random_source.uniform(0.6, 0.8)

    if vertical_amplitude == 0:
        control_1_y = 0.0
        control_2_y = 0.0
        end_y = 0.0
    else:
        direction = random_source.choice((-1.0, 1.0))
        control_1_y = (
            direction
            * vertical_amplitude
            * random_source.uniform(0.35, 0.75)
        )
        end_y_limit = min(1.5, vertical_amplitude * 0.3)
        end_y = random_source.uniform(-end_y_limit, end_y_limit)
        control_2_y = (
            direction
            * vertical_amplitude
            * random_source.uniform(0.35, 0.75)
        )

    points: list[TrajectoryPoint] = []
    for index in range(point_count):
        normalized_time = index / (point_count - 1)
        progress = (
            10 * normalized_time**3
            - 15 * normalized_time**4
            + 6 * normalized_time**5
        )
        inverse = 1 - progress

        x = (
            3 * inverse**2 * progress * control_1_x
            + 3 * inverse * progress**2 * control_2_x
            + progress**3 * distance_x
        )
        y = (
            3 * inverse**2 * progress * control_1_y
            + 3 * inverse * progress**2 * control_2_y
            + progress**3 * end_y
        )
        points.append(
            TrajectoryPoint(
                elapsed_seconds=normalized_time * duration_seconds,
                x=x,
                y=y,
            )
        )

    points[0] = TrajectoryPoint(0.0, 0.0, 0.0)
    points[-1] = TrajectoryPoint(duration_seconds, distance_x, end_y)
    return points


def _node_attributes(node: dict[str, Any]) -> dict[str, str]:
    """把 CDP DOM 节点的扁平 attributes 数组转换成字典。"""
    attributes = node.get("attributes", [])
    return {
        str(attributes[index]): str(attributes[index + 1])
        for index in range(0, len(attributes) - 1, 2)
    }


def _find_slider_nodes(
    root: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """遍历包含 closed Shadow DOM 的 CDP DOM 树，寻找滑块关键节点。"""
    expected_classes = {
        "slider-img-bg": "background",
        "block-img": "target",
        "slide-btn": "button",
    }
    found: dict[str, dict[str, Any]] = {}
    stack = [root]

    while stack and len(found) < len(expected_classes):
        node = stack.pop()
        class_names = set(_node_attributes(node).get("class", "").split())
        for class_name, result_key in expected_classes.items():
            if class_name in class_names and result_key not in found:
                found[result_key] = node

        for child_key in (
            "children",
            "shadowRoots",
            "pseudoElements",
            "distributedNodes",
        ):
            stack.extend(node.get(child_key, []))

        for child_key in ("contentDocument", "templateContent"):
            child = node.get(child_key)
            if child:
                stack.append(child)

    return found


async def _get_document_tree(cdp_session: Any) -> dict[str, Any]:
    """获取包含 closed Shadow DOM 的完整 CDP DOM 树。"""
    response = await cdp_session.send(
        "DOM.getDocument",
        {"depth": -1, "pierce": True},
    )
    return response["root"]


async def _get_node_box(
    cdp_session: Any,
    node_id: int,
) -> tuple[RenderedSize, tuple[float, float]]:
    """读取节点的视口 CSS 渲染尺寸和中心点。"""
    resolved = await cdp_session.send("DOM.resolveNode", {"nodeId": node_id})
    object_id = resolved["object"]["objectId"]
    try:
        response = await cdp_session.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": """function () {
                    const rect = this.getBoundingClientRect();
                    return {
                        x: rect.x,
                        y: rect.y,
                        width: rect.width,
                        height: rect.height
                    };
                }""",
                "returnByValue": True,
            },
        )
        rect = response["result"]["value"]
    finally:
        try:
            await cdp_session.send(
                "Runtime.releaseObject",
                {"objectId": object_id},
            )
        except Exception:
            pass

    width, height = _validate_rendered_size(
        (rect["width"], rect["height"]),
        "DOM 节点",
    )
    center = (
        float(rect["x"]) + width / 2,
        float(rect["y"]) + height / 2,
    )
    return (width, height), center


async def _read_slider_snapshot(cdp_session: Any) -> _SliderSnapshot:
    """读取滑块图片、渲染尺寸和鼠标按下位置。"""
    root = await _get_document_tree(cdp_session)
    nodes = _find_slider_nodes(root)
    missing_nodes = {"background", "target", "button"} - nodes.keys()
    if missing_nodes:
        raise RuntimeError(
            "closed Shadow DOM 中缺少滑块节点: "
            + "、".join(sorted(missing_nodes))
        )

    background_attributes = _node_attributes(nodes["background"])
    target_attributes = _node_attributes(nodes["target"])
    background_data_url = background_attributes.get("src", "")
    target_data_url = target_attributes.get("src", "")
    if not background_data_url or not target_data_url:
        raise RuntimeError("滑块背景图或缺口图缺少 src Data URL")

    background_size, _ = await _get_node_box(
        cdp_session,
        int(nodes["background"]["nodeId"]),
    )
    target_size, _ = await _get_node_box(
        cdp_session,
        int(nodes["target"]["nodeId"]),
    )
    _, button_center = await _get_node_box(
        cdp_session,
        int(nodes["button"]["nodeId"]),
    )
    return _SliderSnapshot(
        target_data_url=target_data_url,
        background_data_url=background_data_url,
        target_rendered_size=target_size,
        background_rendered_size=background_size,
        button_center=button_center,
    )


async def _slider_button_exists(cdp_session: Any) -> bool:
    """判断当前文档中是否仍存在 closed Shadow DOM 滑块按钮。"""
    root = await _get_document_tree(cdp_session)
    return "button" in _find_slider_nodes(root)


async def solve_closed_shadow_slider(
    page: Page,
    *,
    ready_timeout_seconds: float = 10.0,
    success_timeout_seconds: float = 8.0,
) -> bool:
    """识别并拖动拼多多 closed Shadow DOM 滑块。

    验证通过后的页面仍可能保留 ``mobile.yangkeduo.com`` URL 前缀，
    因此成功条件是滑块按钮连续三次未出现在 CDP DOM 树中。
    """
    cdp_session = await page.context.new_cdp_session(page)
    mouse_is_down = False
    try:
        await cdp_session.send("DOM.enable")
        loop = asyncio.get_running_loop()
        ready_deadline = loop.time() + ready_timeout_seconds
        last_ready_error: Exception | None = None
        while True:
            if page.is_closed():
                return False
            try:
                snapshot = await _read_slider_snapshot(cdp_session)
                break
            except Exception as error:
                last_ready_error = error
                if loop.time() >= ready_deadline:
                    raise RuntimeError(
                        "等待 closed Shadow DOM 滑块渲染超时"
                    ) from last_ready_error
                await asyncio.sleep(0.5)

        distance_x = await calculate_slider_drag_distance_async(
            snapshot.target_data_url,
            snapshot.background_data_url,
            snapshot.target_rendered_size,
            snapshot.background_rendered_size,
        )
        trajectory = generate_drag_trajectory(distance_x)
        start_x, start_y = snapshot.button_center

        await page.mouse.move(start_x, start_y)
        await page.mouse.down()
        mouse_is_down = True

        started_at = loop.time()
        for point in trajectory[1:]:
            remaining_seconds = started_at + point.elapsed_seconds - loop.time()
            if remaining_seconds > 0:
                await asyncio.sleep(remaining_seconds)
            await page.mouse.move(start_x + point.x, start_y + point.y)

        await page.mouse.up()
        mouse_is_down = False

        deadline = loop.time() + success_timeout_seconds
        consecutive_absent_checks = 0
        while loop.time() < deadline:
            if page.is_closed():
                return False
            try:
                slider_exists = await _slider_button_exists(cdp_session)
            except Exception:
                consecutive_absent_checks = 0
                await asyncio.sleep(1)
                continue

            if slider_exists:
                consecutive_absent_checks = 0
            else:
                consecutive_absent_checks += 1
                if consecutive_absent_checks >= 3:
                    return True
            await asyncio.sleep(1)
        return False
    finally:
        if mouse_is_down:
            try:
                await page.mouse.up()
            except Exception:
                pass
        try:
            await cdp_session.detach()
        except Exception:
            pass
