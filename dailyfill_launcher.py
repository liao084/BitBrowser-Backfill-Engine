#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dailyfill 客户实例管理器：生成 .env、更新通用 EXE 并启动客户实例。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil
from dotenv import dotenv_values
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


CONFIG_FILENAME = "dailyfill_launcher_config.json"
WINDOWS_INVALID_NAME = re.compile(r'[<>:"/\\|?*]')

if getattr(sys, "frozen", False):
    runtime_dir = Path(sys.executable).resolve().parent
else:
    runtime_dir = Path(__file__).resolve().parent


def find_launcher_config() -> Path:
    """从 Launcher 同目录下的 dailyfill/_release 读取正式配置。"""
    config_path = (
        runtime_dir
        / "dailyfill"
        / "_release"
        / CONFIG_FILENAME
    ).resolve()
    if config_path.is_file():
        return config_path
    raise FileNotFoundError(
        f"未找到 {config_path}。请确认 Launcher 同目录存在 "
        "dailyfill\\_release\\dailyfill_launcher_config.json。"
    )


def load_launcher_config(config_path: Path) -> dict[str, Any]:
    """读取 Launcher 配置，并补齐部署目录和默认值。"""
    with config_path.open("r", encoding="utf-8") as file_handle:
        config = json.load(file_handle)
    if not isinstance(config, dict):
        raise ValueError("管理器配置根节点必须是 JSON 对象")

    # Launcher 位于桌面，客户实例统一部署到同目录下的 dailyfill。
    deployment_root = runtime_dir / "dailyfill"
    config["deployment_root"] = str(deployment_root.resolve())
    config.setdefault("engine_filename", "daily_engine.exe")
    config.setdefault("customers", [])
    config.setdefault("platforms", [])
    config.setdefault("defaults", {})

    if not isinstance(config["customers"], list):
        raise ValueError("customers 必须是 JSON 数组")
    if not isinstance(config["platforms"], list) or not config["platforms"]:
        raise ValueError("platforms 必须是非空 JSON 数组")
    return config


def json_env_value(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent)


def build_env_content(values: dict[str, Any]) -> str:
    """生成 daily_engine 实际读取的客户配置。"""
    return "\n".join(
        [
            "# 由 dailyfill_launcher 生成；需要调整时请优先使用管理器。",
            "",
            f"BITE_ID={json_env_value(values['bite_id'])}",
            f"GC_PAGE_URL_MARKERS={json_env_value(values['markers'])}",
            f"CUSTOMER_NAME={json_env_value(values['customer_name'])}",
            f"REPORT_READY_TIME={values['report_ready_time']}",
            "",
            "WORKER_HEARTBEAT_SILENCE_SECONDS="
            f"{values['worker_heartbeat_seconds']}",
            "BUSINESS_HEARTBEAT_SILENCE_SECONDS="
            f"{values['business_heartbeat_seconds']}",
            "",
            f"WORKER_COUNT={values['worker_count']}",
            f"MAX_ATTEMPTS={values['max_attempts']}",
            "KEEP_BROWSER_AFTER_RUN="
            f"{'true' if values['keep_browser_after_run'] else 'false'}",
            f"TARGET_DATE_OFFSET_DAYS={values['target_date_offset_days']}",
            "TARGET_DATE=",
            f"COOKIE_DIR={json_env_value(values['cookie_dir'])}",
            f"TASK_URL={json_env_value(values['task_url'])}",
            "DAILY_TASKS='"
            + json_env_value(values["tasks"], indent=4)
            + "'",
            "PLATFORMS='"
            + json_env_value(values["platforms"], indent=4)
            + "'",
            "",
        ]
    )


def write_text_atomically(path: Path, content: str) -> None:
    """先完整写入临时文件，再原子替换正式配置。"""
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.write_text(content, encoding="utf-8")
    os.replace(temporary_path, path)


def same_executable_is_running(executable_path: Path) -> bool:
    """Windows 下按完整路径判断当前客户实例是否已运行。"""
    if os.name != "nt":
        return False

    expected = os.path.normcase(str(executable_path.resolve()))
    for process in psutil.process_iter(["exe"]):
        try:
            actual = process.info.get("exe")
            if actual and os.path.normcase(str(Path(actual).resolve())) == expected:
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    return False


class DailyfillLauncherWindow(QMainWindow):
    """编辑 Dailyfill 客户实例配置的单窗口界面。"""

    def __init__(self, config_path: Path, config: dict[str, Any]):
        super().__init__()
        self.config_path = config_path
        self.config = config
        self.deployment_root = Path(config["deployment_root"])
        self.release_dir = self.deployment_root / "_release"
        self.engine_filename = str(config["engine_filename"])
        self.customers = {
            str(customer.get("name", "")).strip(): customer
            for customer in config["customers"]
            if isinstance(customer, dict) and str(customer.get("name", "")).strip()
        }
        self.platforms = [
            platform
            for platform in config["platforms"]
            if isinstance(platform, dict)
            and str(platform.get("name", "")).strip()
            and isinstance(platform.get("markers"), list)
            and str(platform.get("home_url", "")).strip()
        ]
        if not self.platforms:
            raise ValueError("platforms 中没有可用的平台配置")

        self.platform_checks: dict[str, QCheckBox] = {}
        self.customer_dirs: dict[str, Path] = {}
        self.cookie_dir = ""
        self.task_url = ""
        self.report_ready_time = ""
        self.default_target_offset_days = 1

        self.setWindowTitle("Dailyfill 客户实例管理器")
        self.resize(600, 720)
        self.setMinimumSize(600, 640)
        self._build_ui()
        self._load_customer_choices()
        self._on_customer_changed()

    def _build_ui(self) -> None:
        central_widget = QWidget()
        root_layout = QVBoxLayout(central_widget)
        root_layout.setContentsMargins(14, 12, 14, 12)
        root_layout.setSpacing(9)

        title = QLabel("Dailyfill 客户实例管理器")
        title.setStyleSheet("font-size: 19px; font-weight: 600;")
        deployment_label = QLabel(f"部署目录：{self.deployment_root}")
        config_label = QLabel(f"配置文件：{self.config_path}")
        for label in (deployment_label, config_label):
            label.setStyleSheet("color: #666;")
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setWordWrap(True)

        root_layout.addWidget(title)
        root_layout.addWidget(deployment_label)
        root_layout.addWidget(config_label)
        root_layout.addWidget(self._build_customer_group())

        configuration_layout = QHBoxLayout()
        configuration_layout.setSpacing(9)
        configuration_layout.addWidget(self._build_platform_group(), 1)
        configuration_layout.addWidget(self._build_daily_group(), 2)
        root_layout.addLayout(configuration_layout)
        root_layout.addWidget(self._build_task_table_group(), 1)
        root_layout.addLayout(self._build_action_bar())
        self.setCentralWidget(central_widget)

    def _build_customer_group(self) -> QGroupBox:
        group = QGroupBox("客户与运行参数")
        layout = QGridLayout(group)

        self.customer_combo = QComboBox()
        self.customer_combo.setEditable(True)
        self.customer_combo.currentIndexChanged.connect(
            self._on_customer_changed
        )
        self.bite_id_edit = QLineEdit()

        self.worker_heartbeat_spin = QSpinBox()
        self.worker_heartbeat_spin.setRange(1, 86400)
        self.worker_heartbeat_spin.setSuffix(" 秒")
        self.business_heartbeat_spin = QSpinBox()
        self.business_heartbeat_spin.setRange(1, 86400)
        self.business_heartbeat_spin.setSuffix(" 秒")

        new_button = QPushButton("新建客户")
        new_button.clicked.connect(self.start_new_customer)
        load_button = QPushButton("加载已有 .env")
        load_button.clicked.connect(self.load_customer_env)
        folder_button = QPushButton("打开客户目录")
        folder_button.clicked.connect(self.open_customer_folder)

        layout.addWidget(QLabel("客户"), 0, 0)
        layout.addWidget(self.customer_combo, 0, 1, 1, 3)
        layout.addWidget(new_button, 1, 1)
        layout.addWidget(load_button, 1, 2)
        layout.addWidget(folder_button, 1, 3)
        layout.addWidget(QLabel("BITE_ID"), 2, 0)
        layout.addWidget(self.bite_id_edit, 2, 1, 1, 3)
        layout.addWidget(QLabel("Worker 心跳静默"), 3, 0)
        layout.addWidget(self.worker_heartbeat_spin, 3, 1)
        layout.addWidget(QLabel("业务页心跳静默"), 3, 2)
        layout.addWidget(self.business_heartbeat_spin, 3, 3)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        return group

    def _build_platform_group(self) -> QGroupBox:
        group = QGroupBox("业务平台")
        group.setMaximumWidth(210)
        layout = QVBoxLayout(group)

        for platform in self.platforms:
            name = str(platform["name"])
            checkbox = QCheckBox(name)
            self.platform_checks[name] = checkbox
            layout.addWidget(checkbox)

        self.custom_markers_edit = QLineEdit()
        self.custom_markers_edit.setPlaceholderText("多个标识用英文逗号分隔")
        layout.addStretch()
        layout.addWidget(QLabel("其他 URL 标识"))
        layout.addWidget(self.custom_markers_edit)
        return group

    def _build_daily_group(self) -> QGroupBox:
        group = QGroupBox("每日任务与调度参数")
        layout = QGridLayout(group)

        self.card_id_spin = QSpinBox()
        self.card_id_spin.setRange(1, 2_147_483_647)
        self.worker_count_spin = QSpinBox()
        self.worker_count_spin.setRange(1, 20)
        self.max_attempts_spin = QSpinBox()
        self.max_attempts_spin.setRange(1, 20)
        self.target_offset_spin = QSpinBox()
        self.target_offset_spin.setRange(0, 365)
        self.target_offset_spin.setSuffix(" 天")
        self.keep_browser_check = QCheckBox("任务完成后保留浏览器")

        add_button = QPushButton("新增任务 ID")
        add_button.clicked.connect(self.add_task)
        update_button = QPushButton("更新选中任务")
        update_button.clicked.connect(self.update_selected_task)
        delete_button = QPushButton("删除选中任务")
        delete_button.clicked.connect(self.delete_selected_task)

        layout.addWidget(QLabel("任务卡片 ID"), 0, 0)
        layout.addWidget(self.card_id_spin, 0, 1, 1, 3)
        layout.addWidget(QLabel("Worker 数量"), 1, 0)
        layout.addWidget(self.worker_count_spin, 1, 1)
        layout.addWidget(QLabel("最大尝试次数"), 1, 2)
        layout.addWidget(self.max_attempts_spin, 1, 3)
        layout.addWidget(QLabel("目标日期偏移"), 2, 0)
        layout.addWidget(self.target_offset_spin, 2, 1)
        layout.addWidget(self.keep_browser_check, 2, 2, 1, 2)
        actions_layout = QHBoxLayout()
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.addWidget(add_button, 1)
        actions_layout.addWidget(update_button, 1)
        actions_layout.addWidget(delete_button, 1)
        layout.addLayout(actions_layout, 3, 0, 1, 4)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        return group

    def _build_task_table_group(self) -> QGroupBox:
        group = QGroupBox("每日任务清单")
        layout = QVBoxLayout(group)
        self.task_table = QTableWidget(0, 2)
        self.task_table.setHorizontalHeaderLabels(
            ["任务卡片 ID", "目标日期偏移"]
        )
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self.task_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        self.task_table.itemSelectionChanged.connect(
            self._load_selected_task_into_editor
        )
        layout.addWidget(self.task_table)
        return group

    def _build_action_bar(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        self.status_label = QLabel("请填写配置并添加至少一个任务 ID。")
        self.status_label.setStyleSheet("color: #555;")

        save_button = QPushButton("保存配置")
        save_button.clicked.connect(lambda: self.save_instance(start=False))
        start_button = QPushButton("保存配置并启动")
        start_button.setStyleSheet(
            "QPushButton { background: #2563eb; color: white; "
            "font-weight: 600; padding: 7px 18px; }"
        )
        start_button.clicked.connect(lambda: self.save_instance(start=True))

        layout.addWidget(self.status_label, 1)
        layout.addWidget(save_button)
        layout.addWidget(start_button)
        return layout

    def _load_customer_choices(self) -> None:
        current_name = self.customer_combo.currentText().strip()
        names = set(self.customers)
        discovered_dirs: dict[str, Path] = {}
        if self.deployment_root.is_dir():
            for env_path in self.deployment_root.rglob(".env"):
                if self.release_dir in env_path.parents:
                    continue
                customer_dir = env_path.parent.resolve()
                customer_name = customer_dir.name
                existing_dir = discovered_dirs.get(customer_name)
                if existing_dir is not None and existing_dir != customer_dir:
                    raise ValueError(
                        f"发现重名客户目录 {customer_name!r}："
                        f"{existing_dir} 与 {customer_dir}。"
                        "请先修改其中一个客户目录名称。"
                    )
                discovered_dirs[customer_name] = customer_dir
                names.add(customer_name)
        self.customer_dirs = discovered_dirs

        self.customer_combo.blockSignals(True)
        try:
            self.customer_combo.clear()
            self.customer_combo.addItems(sorted(names))
            if current_name:
                self.customer_combo.setCurrentText(current_name)
        finally:
            self.customer_combo.blockSignals(False)

    def _apply_defaults(self, *, apply_customer: bool = True) -> None:
        defaults = self.config["defaults"]
        self.worker_heartbeat_spin.setValue(
            int(defaults.get("worker_heartbeat_seconds", 120))
        )
        self.business_heartbeat_spin.setValue(
            int(defaults.get("business_heartbeat_seconds", 180))
        )
        self.worker_count_spin.setValue(int(defaults.get("worker_count", 1)))
        self.max_attempts_spin.setValue(int(defaults.get("max_attempts", 5)))
        self.default_target_offset_days = int(
            defaults.get("target_date_offset_days", 1)
        )
        self.target_offset_spin.setValue(self.default_target_offset_days)
        self.keep_browser_check.setChecked(
            bool(defaults.get("keep_browser_after_run", True))
        )
        self.cookie_dir = str(
            defaults.get("cookie_dir", "C:/Users/Administrator/Desktop/COOKIE")
        )
        self.task_url = str(defaults.get("task_url", ""))
        self.report_ready_time = str(
            defaults.get("report_ready_time", "")
        ).strip()
        if apply_customer:
            self._apply_selected_customer_defaults()

    def _apply_selected_customer_defaults(self) -> None:
        customer = self.customers.get(self.customer_combo.currentText().strip())
        if not customer:
            return

        self.bite_id_edit.setText(str(customer.get("bite_id", "")))
        default_platforms = {str(name) for name in customer.get("platforms", [])}
        for name, checkbox in self.platform_checks.items():
            checkbox.setChecked(name in default_platforms)

        if "worker_count" in customer:
            self.worker_count_spin.setValue(int(customer["worker_count"]))
        if "max_attempts" in customer:
            self.max_attempts_spin.setValue(int(customer["max_attempts"]))
        if "target_date_offset_days" in customer:
            self.default_target_offset_days = int(
                customer["target_date_offset_days"]
            )
            self.target_offset_spin.setValue(self.default_target_offset_days)
        if "report_ready_time" in customer:
            self.report_ready_time = str(
                customer["report_ready_time"]
            ).strip()

    def _clear_customer_state(self) -> None:
        """清除上一个客户的专属内容，避免切换后残留旧配置。"""
        self.bite_id_edit.clear()
        self.task_table.setRowCount(0)
        self.card_id_spin.setValue(1)
        self.custom_markers_edit.clear()
        for checkbox in self.platform_checks.values():
            checkbox.setChecked(False)

    def _on_customer_changed(self, _index: int | None = None) -> None:
        """切换客户时先应用权威配置，再自动载入该客户的 .env。"""
        self._clear_customer_state()
        self._apply_defaults()
        env_path = self._customer_dir() / ".env"
        if env_path.is_file():
            self._load_customer_env_file(env_path)

    def start_new_customer(self) -> None:
        """清空客户专属数据，并恢复 Launcher 的通用默认设置。"""
        self.customer_combo.blockSignals(True)
        try:
            self.customer_combo.setCurrentIndex(-1)
            self.customer_combo.clearEditText()
        finally:
            self.customer_combo.blockSignals(False)

        self._clear_customer_state()
        self._apply_defaults(apply_customer=False)
        self.status_label.setText("新建客户：请填写客户名称、BITE_ID 和任务清单。")
        self.customer_combo.setFocus()

    def _current_task(self) -> dict[str, int]:
        return {
            "card_id": self.card_id_spin.value(),
            "target_date_offset_days": self.target_offset_spin.value(),
        }

    def _task_at_row(self, row: int) -> dict[str, int]:
        offset_item = self.task_table.item(row, 1)
        offset = offset_item.data(Qt.ItemDataRole.UserRole)
        return {
            "card_id": int(self.task_table.item(row, 0).text()),
            "target_date_offset_days": int(offset),
        }

    def _all_tasks(self) -> list[dict[str, int]]:
        return [
            self._task_at_row(row)
            for row in range(self.task_table.rowCount())
        ]

    def _append_task_row(self, task: dict[str, Any]) -> None:
        row = self.task_table.rowCount()
        self.task_table.insertRow(row)
        offset = int(
            task.get(
                "target_date_offset_days",
                self.default_target_offset_days,
            )
        )
        card_item = QTableWidgetItem(str(int(task["card_id"])))
        card_item.setTextAlignment(Qt.AlignCenter)
        offset_item = QTableWidgetItem(self._target_offset_text(offset))
        offset_item.setTextAlignment(Qt.AlignCenter)
        offset_item.setData(Qt.ItemDataRole.UserRole, offset)
        self.task_table.setItem(row, 0, card_item)
        self.task_table.setItem(row, 1, offset_item)

    @staticmethod
    def _target_offset_text(offset: int) -> str:
        return "当天" if offset == 0 else f"{offset} 天前"

    def add_task(self) -> None:
        task = self._current_task()
        if any(
            existing["card_id"] == task["card_id"]
            for existing in self._all_tasks()
        ):
            QMessageBox.warning(self, "任务重复", "该任务 ID 已存在。")
            return
        self._append_task_row(task)
        self.status_label.setText("任务 ID 已添加。")

    def update_selected_task(self) -> None:
        row = self.task_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择任务", "请先选择需要更新的任务行。")
            return

        task = self._current_task()
        other_tasks = [
            self._task_at_row(index)
            for index in range(self.task_table.rowCount())
            if index != row
        ]
        if any(
            existing["card_id"] == task["card_id"]
            for existing in other_tasks
        ):
            QMessageBox.warning(self, "任务重复", "该任务 ID 已存在。")
            return
        self.task_table.item(row, 0).setText(str(task["card_id"]))
        offset_item = self.task_table.item(row, 1)
        offset_item.setText(
            self._target_offset_text(task["target_date_offset_days"])
        )
        offset_item.setData(
            Qt.ItemDataRole.UserRole,
            task["target_date_offset_days"],
        )
        self.status_label.setText("选中任务 ID 和日期偏移已更新。")

    def delete_selected_task(self) -> None:
        row = self.task_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择任务", "请先选择需要删除的任务行。")
            return
        self.task_table.removeRow(row)
        self.status_label.setText("选中任务 ID 已删除。")

    def _load_selected_task_into_editor(self) -> None:
        row = self.task_table.currentRow()
        if row >= 0:
            task = self._task_at_row(row)
            self.card_id_spin.setValue(task["card_id"])
            self.target_offset_spin.setValue(
                task["target_date_offset_days"]
            )

    def _selected_platform_values(
        self,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        markers: list[str] = []
        selected_platforms: list[dict[str, Any]] = []

        for platform in self.platforms:
            name = str(platform["name"])
            if not self.platform_checks[name].isChecked():
                continue

            for marker in platform["markers"]:
                normalized = str(marker).strip()
                if normalized and normalized not in markers:
                    markers.append(normalized)

            selected_platforms.append(
                {
                    key: value
                    for key, value in platform.items()
                    if key != "markers"
                }
            )

        for marker in self.custom_markers_edit.text().split(","):
            normalized = marker.strip()
            if normalized and normalized not in markers:
                markers.append(normalized)
        return markers, selected_platforms

    def _validate_instance(self) -> dict[str, Any]:
        customer_name = self.customer_combo.currentText().strip()
        if not customer_name:
            raise ValueError("客户名称不能为空")
        if WINDOWS_INVALID_NAME.search(customer_name):
            raise ValueError('客户名称不能包含 <>:"/\\|?* 等 Windows 非法字符')
        if customer_name.endswith((" ", ".")):
            raise ValueError("客户名称不能以空格或句点结尾")

        bite_id = self.bite_id_edit.text().strip()
        if not bite_id:
            raise ValueError("BITE_ID 不能为空")

        markers, platforms = self._selected_platform_values()
        if not platforms:
            raise ValueError("请至少选择一个业务平台")
        if not markers:
            raise ValueError("业务执行页 URL 标识不能为空")

        tasks = self._all_tasks()
        if not tasks:
            raise ValueError("请至少添加一个任务卡片 ID")
        if self.business_heartbeat_spin.value() <= self.worker_heartbeat_spin.value():
            raise ValueError("业务页心跳静默必须大于 Worker 心跳静默")
        if not self.cookie_dir.strip():
            raise ValueError("Launcher 配置中的 cookie_dir 不能为空")
        if not self.task_url.strip():
            raise ValueError("Launcher 配置中的 task_url 不能为空")
        if not self.report_ready_time:
            raise ValueError(
                "Launcher 配置中的 report_ready_time 不能为空"
            )
        if re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d",
            self.report_ready_time,
        ) is None:
            raise ValueError(
                "Launcher 配置中的 report_ready_time 必须使用 HH:MM 格式"
            )

        return {
            "customer_name": customer_name,
            "bite_id": bite_id,
            "report_ready_time": self.report_ready_time,
            "markers": markers,
            "platforms": platforms,
            "tasks": tasks,
            "worker_heartbeat_seconds": self.worker_heartbeat_spin.value(),
            "business_heartbeat_seconds": self.business_heartbeat_spin.value(),
            "worker_count": self.worker_count_spin.value(),
            "max_attempts": self.max_attempts_spin.value(),
            "target_date_offset_days": self.default_target_offset_days,
            "keep_browser_after_run": self.keep_browser_check.isChecked(),
            "cookie_dir": self.cookie_dir,
            "task_url": self.task_url,
        }

    def _customer_dir(self, customer_name: str | None = None) -> Path:
        name = customer_name or self.customer_combo.currentText().strip()
        if name in self.customer_dirs:
            return self.customer_dirs[name]
        return self.deployment_root / name

    def load_customer_env(self) -> None:
        env_path = self._customer_dir() / ".env"
        if not env_path.is_file():
            QMessageBox.information(
                self,
                "尚无客户配置",
                f"未找到 {env_path}，保存后将创建新客户实例。",
            )
            return

        self._load_customer_env_file(env_path)

    def _load_customer_env_file(self, env_path: Path) -> None:
        """载入客户可编辑项；权威字段始终保留 Launcher 配置值。"""

        try:
            values = dotenv_values(env_path)
            self.bite_id_edit.setText(str(values.get("BITE_ID") or ""))
            self.worker_heartbeat_spin.setValue(
                int(values.get("WORKER_HEARTBEAT_SILENCE_SECONDS") or 120)
            )
            self.business_heartbeat_spin.setValue(
                int(values.get("BUSINESS_HEARTBEAT_SILENCE_SECONDS") or 180)
            )
            self.worker_count_spin.setValue(int(values.get("WORKER_COUNT") or 1))
            self.max_attempts_spin.setValue(int(values.get("MAX_ATTEMPTS") or 5))
            self.default_target_offset_days = int(
                values.get("TARGET_DATE_OFFSET_DAYS") or 1
            )
            self.target_offset_spin.setValue(self.default_target_offset_days)
            self.keep_browser_check.setChecked(
                str(values.get("KEEP_BROWSER_AFTER_RUN") or "true").lower()
                in {"true", "1", "yes"}
            )
            self.cookie_dir = str(values.get("COOKIE_DIR") or self.cookie_dir)

            selected_platforms = json.loads(
                str(values.get("PLATFORMS") or "[]")
            )
            selected_names = {
                str(platform.get("name", ""))
                for platform in selected_platforms
                if isinstance(platform, dict)
            }
            for name, checkbox in self.platform_checks.items():
                checkbox.setChecked(name in selected_names)

            markers = json.loads(
                str(values.get("GC_PAGE_URL_MARKERS") or "[]")
            )
            known_markers = {
                str(marker)
                for platform in self.platforms
                if str(platform["name"]) in selected_names
                for marker in platform["markers"]
            }
            custom_markers = [
                str(marker)
                for marker in markers
                if str(marker) not in known_markers
            ]
            self.custom_markers_edit.setText(", ".join(custom_markers))

            tasks = json.loads(str(values.get("DAILY_TASKS") or "[]"))
            self.task_table.setRowCount(0)
            for task in tasks:
                if not isinstance(task, dict) or "card_id" not in task:
                    raise ValueError("DAILY_TASKS 中存在缺少 card_id 的任务")
                self._append_task_row(task)
            self.status_label.setText(f"已加载：{env_path}")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.critical(self, "加载失败", f"无法读取客户配置：{error}")

    def save_instance(self, *, start: bool) -> None:
        try:
            values = self._validate_instance()
            customer_dir = self._customer_dir(values["customer_name"])
            customer_dir.mkdir(parents=True, exist_ok=True)
            env_path = customer_dir / ".env"
            write_text_atomically(env_path, build_env_content(values))

            source_engine = self.release_dir / self.engine_filename
            target_engine = customer_dir / self.engine_filename
            running = same_executable_is_running(target_engine)

            engine_updated = False
            if source_engine.is_file() and not running:
                temporary_engine = target_engine.with_name(
                    f"{target_engine.name}.tmp"
                )
                shutil.copy2(source_engine, temporary_engine)
                os.replace(temporary_engine, target_engine)
                engine_updated = True

            if not start:
                if running:
                    detail = "配置已保存；客户实例正在运行，未覆盖其 EXE。"
                elif engine_updated:
                    detail = "配置已保存，并已同步 _release 中的最新 EXE。"
                else:
                    detail = f"配置已保存；未找到 {source_engine}，未复制 EXE。"
                self.status_label.setText(detail)
                QMessageBox.information(self, "保存成功", f"{detail}\n\n{env_path}")
                self._load_customer_choices()
                return

            if running:
                raise RuntimeError("该客户的 daily_engine.exe 已经在运行")
            if not target_engine.is_file():
                raise FileNotFoundError(
                    f"未找到 {source_engine}，请先放入最新 Dailyfill EXE"
                )

            creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(
                [str(target_engine)],
                cwd=str(customer_dir),
                creationflags=creation_flags,
            )
            self.status_label.setText("配置已保存，Dailyfill 客户实例已启动。")
            self.close()
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.critical(self, "操作失败", str(error))

    def open_customer_folder(self) -> None:
        customer_name = self.customer_combo.currentText().strip()
        if not customer_name:
            QMessageBox.information(self, "未选择客户", "请先选择或输入客户名称。")
            return
        customer_dir = self._customer_dir(customer_name)
        customer_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(customer_dir)))


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("Dailyfill Launcher")
    application.setStyle("Fusion")

    try:
        config_path = find_launcher_config()
        config = load_launcher_config(config_path)
        window = DailyfillLauncherWindow(config_path, config)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        QMessageBox.critical(None, "启动失败", str(error))
        return 1

    window.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
