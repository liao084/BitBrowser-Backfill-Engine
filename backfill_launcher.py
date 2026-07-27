#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill 客户实例管理器：生成 .env、更新通用 EXE 并启动客户实例。"""

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
from PySide6.QtCore import QDate, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
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


CONFIG_FILENAME = "backfill_launcher_config.json"
WINDOWS_INVALID_NAME = re.compile(r'[<>:"/\\|?*]')

if getattr(sys, "frozen", False):
    runtime_dir = Path(sys.executable).resolve().parent
else:
    runtime_dir = Path(__file__).resolve().parent


def find_launcher_config() -> Path:
    """兼容源码本地调试和桌面 EXE 两种配置位置。"""
    candidates = [
        runtime_dir / CONFIG_FILENAME,
        runtime_dir / "_release" / CONFIG_FILENAME,
        Path.cwd() / CONFIG_FILENAME,
        Path.cwd() / "_release" / CONFIG_FILENAME,
        Path.home() / "Desktop" / "backfill" / "_release" / CONFIG_FILENAME,
    ]
    checked: set[Path] = set()
    for candidate in candidates:
        normalized = candidate.resolve()
        if normalized in checked:
            continue
        checked.add(normalized)
        if normalized.is_file():
            return normalized
    raise FileNotFoundError(
        "未找到 backfill_launcher_config.json。"
        "源码调试时请放在 backfill_launcher.py 同目录；"
        "正式运行时请放在桌面 backfill/_release 目录。"
    )


def load_launcher_config(config_path: Path) -> dict[str, Any]:
    """读取管理器配置，并补齐部署目录和默认值。"""
    with config_path.open("r", encoding="utf-8") as file_handle:
        config = json.load(file_handle)
    if not isinstance(config, dict):
        raise ValueError("管理器配置根节点必须是 JSON 对象")

    deployment_root_raw = str(config.get("deployment_root", "")).strip()
    if deployment_root_raw:
        deployment_root = Path(deployment_root_raw).expanduser()
    elif config_path.parent.name == "_release":
        deployment_root = config_path.parent.parent
    else:
        deployment_root = config_path.parent / "backfill_instances"

    config["deployment_root"] = str(deployment_root.resolve())
    config.setdefault("engine_filename", "backfill_engine.exe")
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
    """只生成历史补采实际读取的配置。"""
    return "\n".join(
        [
            "# 由 backfill_launcher 生成；需要调整时请优先使用管理器。",
            "",
            f"BROWSER_TYPE={values['browser_type']}",
            f"BITE_ID={json_env_value(values['bite_id'])}",
            f"CDP_ADDRESS={values['cdp_address']}",
            "",
            f"GC_PAGE_URL_MARKERS={json_env_value(values['markers'])}",
            f"CUSTOMER_NAME={json_env_value(values['customer_name'])}",
            "",
            "TASKS_CONFIG='"
            + json_env_value(values["tasks"], indent=4)
            + "'",
            "",
            "WORKER_HEARTBEAT_SILENCE_SECONDS="
            f"{values['worker_heartbeat_seconds']}",
            "BUSINESS_HEARTBEAT_SILENCE_SECONDS="
            f"{values['business_heartbeat_seconds']}",
            "",
        ]
    )


def write_text_atomically(path: Path, content: str) -> None:
    """先完整写入临时文件，再替换正式配置。"""
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


class BackfillLauncherWindow(QMainWindow):
    """编辑客户实例配置的单窗口界面。"""

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
        ]
        self.platform_checks: dict[str, QCheckBox] = {}

        self.setWindowTitle("Backfill 客户实例管理器")
        self.resize(600, 780)
        self.setMinimumSize(600, 680)
        self._build_ui()
        self._load_customer_choices()
        self._apply_defaults()

    def _build_ui(self) -> None:
        central_widget = QWidget()
        root_layout = QVBoxLayout(central_widget)
        root_layout.setContentsMargins(14, 12, 14, 12)
        root_layout.setSpacing(9)

        title = QLabel("Backfill 客户实例管理器")
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
        configuration_layout.addWidget(self._build_task_editor_group(), 2)
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
            self._apply_selected_customer_defaults
        )

        self.browser_type_combo = QComboBox()
        self.browser_type_combo.addItem("BitBrowser", "bitbrowser")
        self.browser_type_combo.addItem("外部 Chromium（CDP）", "external_cdp")
        self.browser_type_combo.currentIndexChanged.connect(
            self._update_browser_fields
        )

        self.bite_id_edit = QLineEdit()
        self.cdp_address_edit = QLineEdit()
        self.cdp_address_edit.setPlaceholderText("127.0.0.1:9222")

        self.worker_heartbeat_spin = QSpinBox()
        self.worker_heartbeat_spin.setRange(1, 86400)
        self.worker_heartbeat_spin.setSuffix(" 秒")

        self.business_heartbeat_spin = QSpinBox()
        self.business_heartbeat_spin.setRange(1, 86400)
        self.business_heartbeat_spin.setSuffix(" 秒")

        load_button = QPushButton("加载已有 .env")
        load_button.clicked.connect(self.load_customer_env)
        folder_button = QPushButton("打开客户目录")
        folder_button.clicked.connect(self.open_customer_folder)

        layout.addWidget(QLabel("客户"), 0, 0)
        layout.addWidget(self.customer_combo, 0, 1, 1, 3)
        layout.addWidget(load_button, 1, 1)
        layout.addWidget(folder_button, 1, 2, 1, 2)
        layout.addWidget(QLabel("浏览器来源"), 2, 0)
        layout.addWidget(self.browser_type_combo, 2, 1, 1, 3)
        layout.addWidget(QLabel("BITE_ID"), 3, 0)
        layout.addWidget(self.bite_id_edit, 3, 1, 1, 3)
        layout.addWidget(QLabel("CDP 地址"), 4, 0)
        layout.addWidget(self.cdp_address_edit, 4, 1, 1, 3)
        layout.addWidget(QLabel("Worker 心跳静默"), 5, 0)
        layout.addWidget(self.worker_heartbeat_spin, 5, 1)
        layout.addWidget(QLabel("业务页心跳静默"), 5, 2)
        layout.addWidget(self.business_heartbeat_spin, 5, 3)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        return group

    def _build_platform_group(self) -> QGroupBox:
        group = QGroupBox("业务平台（用于识别业务执行页）")
        group.setMaximumWidth(210)
        layout = QVBoxLayout(group)

        for platform in self.platforms:
            name = str(platform["name"])
            checkbox = QCheckBox(name)
            self.platform_checks[name] = checkbox
            layout.addWidget(checkbox)

        self.custom_markers_edit = QLineEdit()
        self.custom_markers_edit.setPlaceholderText(
            "多个标识用英文逗号分隔"
        )
        layout.addStretch()
        layout.addWidget(QLabel("其他 URL 标识"))
        layout.addWidget(self.custom_markers_edit)
        return group

    def _build_task_editor_group(self) -> QGroupBox:
        group = QGroupBox("历史补采任务配置")
        layout = QGridLayout(group)

        self.card_spin = QSpinBox()
        self.card_spin.setRange(1, 9999)
        self.card_spin.setMaximumWidth(110)
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.latest_date_check = QCheckBox("截至最新完整日期（昨天）")
        self.latest_date_check.setChecked(True)
        self.latest_date_check.toggled.connect(self._update_latest_date)
        self.chunk_days_spin = QSpinBox()
        self.chunk_days_spin.setRange(1, 366)
        self.chunk_days_spin.setValue(1)
        self.chunk_days_spin.setMaximumWidth(110)

        add_button = QPushButton("新增任务")
        add_button.clicked.connect(self.add_task)
        update_button = QPushButton("更新选中任务")
        update_button.clicked.connect(self.update_selected_task)
        copy_button = QPushButton("复制到编辑区")
        copy_button.clicked.connect(self.copy_selected_task)
        delete_button = QPushButton("删除选中任务")
        delete_button.clicked.connect(self.delete_selected_task)

        layout.addWidget(QLabel("卡片编号"), 0, 0)
        layout.addWidget(self.card_spin, 0, 1)
        layout.addWidget(QLabel("单个区块天数"), 0, 2)
        layout.addWidget(self.chunk_days_spin, 0, 3)
        layout.addWidget(QLabel("开始日期"), 1, 0)
        layout.addWidget(self.start_date_edit, 1, 1, 1, 3)
        layout.addWidget(QLabel("结束日期"), 2, 0)
        layout.addWidget(self.end_date_edit, 2, 1, 1, 3)
        layout.addWidget(self.latest_date_check, 3, 1, 1, 3)
        layout.addWidget(add_button, 4, 0, 1, 2)
        layout.addWidget(update_button, 4, 2, 1, 2)
        layout.addWidget(copy_button, 5, 0, 1, 2)
        layout.addWidget(delete_button, 5, 2, 1, 2)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        return group

    def _build_task_table_group(self) -> QGroupBox:
        group = QGroupBox("任务清单")
        layout = QVBoxLayout(group)
        self.task_table = QTableWidget(0, 4)
        self.task_table.setHorizontalHeaderLabels(
            ["Card", "开始日期", "结束日期", "区块天数"]
        )
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.task_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        self.task_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch
        )
        self.task_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeToContents
        )
        self.task_table.itemSelectionChanged.connect(
            self._load_selected_task_into_editor
        )
        layout.addWidget(self.task_table)
        return group

    def _build_action_bar(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        self.status_label = QLabel("请填写配置并添加至少一条任务。")
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
        names = set(self.customers)
        if self.deployment_root.is_dir():
            for child in self.deployment_root.iterdir():
                if child.is_dir() and child.name != "_release":
                    names.add(child.name)
        self.customer_combo.clear()
        self.customer_combo.addItems(sorted(names))

    def _apply_defaults(self) -> None:
        defaults = self.config["defaults"]
        self.cdp_address_edit.setText(
            str(defaults.get("cdp_address", "127.0.0.1:9222"))
        )
        self.worker_heartbeat_spin.setValue(
            int(defaults.get("worker_heartbeat_seconds", 120))
        )
        self.business_heartbeat_spin.setValue(
            int(defaults.get("business_heartbeat_seconds", 180))
        )

        start_date = QDate.fromString(
            str(defaults.get("task_start", "2025-01-01")),
            "yyyy-MM-dd",
        )
        self.start_date_edit.setDate(
            start_date if start_date.isValid() else QDate.currentDate().addYears(-1)
        )
        self._update_latest_date(True)
        self._update_browser_fields()
        self._apply_selected_customer_defaults()

    def _apply_selected_customer_defaults(self) -> None:
        customer = self.customers.get(self.customer_combo.currentText().strip())
        if not customer:
            return
        self.bite_id_edit.setText(str(customer.get("bite_id", "")))

        default_platforms = {
            str(name) for name in customer.get("platforms", [])
        }
        if default_platforms:
            for name, checkbox in self.platform_checks.items():
                checkbox.setChecked(name in default_platforms)

    def _update_browser_fields(self) -> None:
        is_bitbrowser = self.browser_type_combo.currentData() == "bitbrowser"
        self.bite_id_edit.setEnabled(is_bitbrowser)
        self.cdp_address_edit.setEnabled(not is_bitbrowser)

    def _update_latest_date(self, checked: bool) -> None:
        self.end_date_edit.setEnabled(not checked)
        if checked:
            self.end_date_edit.setDate(QDate.currentDate().addDays(-1))

    def _current_task(self) -> dict[str, Any]:
        end_date = (
            QDate.currentDate().addDays(-1)
            if self.latest_date_check.isChecked()
            else self.end_date_edit.date()
        )
        return {
            "card": self.card_spin.value(),
            "start": self.start_date_edit.date().toString("yyyy-MM-dd"),
            "end": end_date.toString("yyyy-MM-dd"),
            "chunk_days": self.chunk_days_spin.value(),
        }

    def _validate_task(self, task: dict[str, Any]) -> None:
        start = QDate.fromString(task["start"], "yyyy-MM-dd")
        end = QDate.fromString(task["end"], "yyyy-MM-dd")
        if not start.isValid() or not end.isValid():
            raise ValueError("任务日期格式无效")
        if start > end:
            raise ValueError("任务开始日期不能晚于结束日期")

    def _task_at_row(self, row: int) -> dict[str, Any]:
        return {
            "card": int(self.task_table.item(row, 0).text()),
            "start": self.task_table.item(row, 1).text(),
            "end": self.task_table.item(row, 2).text(),
            "chunk_days": int(self.task_table.item(row, 3).text()),
        }

    def _all_tasks(self) -> list[dict[str, Any]]:
        return [
            self._task_at_row(row)
            for row in range(self.task_table.rowCount())
        ]

    def _append_task_row(self, task: dict[str, Any]) -> None:
        row = self.task_table.rowCount()
        self.task_table.insertRow(row)
        values = (
            task["card"],
            task["start"],
            task["end"],
            task["chunk_days"],
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setTextAlignment(Qt.AlignCenter)
            self.task_table.setItem(row, column, item)

    def add_task(self) -> None:
        try:
            task = self._current_task()
            self._validate_task(task)
            if task in self._all_tasks():
                raise ValueError("任务清单中已经存在完全相同的任务")
            self._append_task_row(task)
            self.status_label.setText("任务已添加。")
        except ValueError as error:
            QMessageBox.warning(self, "任务配置有误", str(error))

    def update_selected_task(self) -> None:
        row = self.task_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择任务", "请先选择需要更新的任务行。")
            return
        try:
            task = self._current_task()
            self._validate_task(task)
            other_tasks = [
                self._task_at_row(index)
                for index in range(self.task_table.rowCount())
                if index != row
            ]
            if task in other_tasks:
                raise ValueError("任务清单中已经存在完全相同的任务")
            values = (
                task["card"],
                task["start"],
                task["end"],
                task["chunk_days"],
            )
            for column, value in enumerate(values):
                self.task_table.item(row, column).setText(str(value))
            self.status_label.setText("选中任务已更新。")
        except ValueError as error:
            QMessageBox.warning(self, "任务配置有误", str(error))

    def copy_selected_task(self) -> None:
        row = self.task_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择任务", "请先选择需要复制的任务行。")
            return
        self._load_task_into_editor(self._task_at_row(row))
        self.task_table.clearSelection()
        self.status_label.setText("任务已复制到编辑区，修改后点击“新增任务”。")

    def delete_selected_task(self) -> None:
        row = self.task_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择任务", "请先选择需要删除的任务行。")
            return
        self.task_table.removeRow(row)
        self.status_label.setText("选中任务已删除。")

    def _load_selected_task_into_editor(self) -> None:
        row = self.task_table.currentRow()
        if row >= 0:
            self._load_task_into_editor(self._task_at_row(row))

    def _load_task_into_editor(self, task: dict[str, Any]) -> None:
        self.card_spin.setValue(int(task["card"]))
        self.start_date_edit.setDate(
            QDate.fromString(str(task["start"]), "yyyy-MM-dd")
        )
        end_date = QDate.fromString(str(task["end"]), "yyyy-MM-dd")
        is_latest = end_date == QDate.currentDate().addDays(-1)
        self.latest_date_check.setChecked(is_latest)
        self.end_date_edit.setDate(end_date)
        self.chunk_days_spin.setValue(int(task["chunk_days"]))

    def _selected_markers(self) -> list[str]:
        markers: list[str] = []
        for platform in self.platforms:
            name = str(platform["name"])
            if self.platform_checks[name].isChecked():
                for marker in platform["markers"]:
                    normalized = str(marker).strip()
                    if normalized and normalized not in markers:
                        markers.append(normalized)

        for marker in self.custom_markers_edit.text().split(","):
            normalized = marker.strip()
            if normalized and normalized not in markers:
                markers.append(normalized)
        return markers

    def _validate_instance(self) -> dict[str, Any]:
        customer_name = self.customer_combo.currentText().strip()
        if not customer_name:
            raise ValueError("客户名称不能为空")
        if WINDOWS_INVALID_NAME.search(customer_name):
            raise ValueError('客户名称不能包含 <>:"/\\|?* 等 Windows 非法字符')
        if customer_name.endswith((" ", ".")):
            raise ValueError("客户名称不能以空格或句点结尾")

        browser_type = str(self.browser_type_combo.currentData())
        bite_id = self.bite_id_edit.text().strip()
        cdp_address = self.cdp_address_edit.text().strip()
        if browser_type == "bitbrowser" and not bite_id:
            raise ValueError("BitBrowser 模式必须填写 BITE_ID")
        if browser_type == "external_cdp" and not cdp_address:
            raise ValueError("外部浏览器模式必须填写 CDP 地址")

        markers = self._selected_markers()
        if not markers:
            raise ValueError("请至少选择一个业务平台或填写一个 URL 标识")

        tasks = self._all_tasks()
        if not tasks:
            raise ValueError("请至少添加一条历史补采任务")
        if self.business_heartbeat_spin.value() <= self.worker_heartbeat_spin.value():
            raise ValueError("业务页心跳静默必须大于 Worker 心跳静默")

        return {
            "customer_name": customer_name,
            "browser_type": browser_type,
            "bite_id": bite_id,
            "cdp_address": cdp_address,
            "markers": markers,
            "tasks": tasks,
            "worker_heartbeat_seconds": self.worker_heartbeat_spin.value(),
            "business_heartbeat_seconds": self.business_heartbeat_spin.value(),
        }

    def _customer_dir(self, customer_name: str | None = None) -> Path:
        name = customer_name or self.customer_combo.currentText().strip()
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

        try:
            values = dotenv_values(env_path)
            browser_type = str(values.get("BROWSER_TYPE") or "bitbrowser")
            browser_index = self.browser_type_combo.findData(browser_type)
            if browser_index >= 0:
                self.browser_type_combo.setCurrentIndex(browser_index)
            self.bite_id_edit.setText(str(values.get("BITE_ID") or ""))
            self.cdp_address_edit.setText(str(values.get("CDP_ADDRESS") or ""))
            self.worker_heartbeat_spin.setValue(
                int(values.get("WORKER_HEARTBEAT_SILENCE_SECONDS") or 120)
            )
            self.business_heartbeat_spin.setValue(
                int(values.get("BUSINESS_HEARTBEAT_SILENCE_SECONDS") or 180)
            )

            markers = json.loads(str(values.get("GC_PAGE_URL_MARKERS") or "[]"))
            known_markers: set[str] = set()
            for platform in self.platforms:
                platform_markers = {
                    str(marker) for marker in platform["markers"]
                }
                selected = bool(platform_markers) and platform_markers.issubset(
                    set(markers)
                )
                self.platform_checks[str(platform["name"])].setChecked(selected)
                if selected:
                    known_markers.update(platform_markers)
            custom_markers = [
                str(marker) for marker in markers if str(marker) not in known_markers
            ]
            self.custom_markers_edit.setText(", ".join(custom_markers))

            tasks = json.loads(str(values.get("TASKS_CONFIG") or "[]"))
            self.task_table.setRowCount(0)
            for task in tasks:
                self._validate_task(task)
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
                    detail = "配置已保存；_release 中暂无 EXE，未执行复制。"
                self.status_label.setText(detail)
                QMessageBox.information(self, "保存成功", f"{detail}\n\n{env_path}")
                self._load_customer_choices()
                return

            if os.name != "nt":
                raise RuntimeError(
                    "当前不是 Windows，配置已经保存，但不能启动 Windows EXE。"
                )
            if running:
                raise RuntimeError("该客户的 backfill_engine.exe 已经在运行")
            if not target_engine.is_file():
                raise FileNotFoundError(
                    f"未找到 {source_engine}，请先放入最新 Backfill EXE"
                )

            creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(
                [str(target_engine)],
                cwd=str(customer_dir),
                creationflags=creation_flags,
            )
            self.status_label.setText("配置已保存，Backfill 客户实例已启动。")
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
    application.setApplicationName("Backfill Launcher")
    application.setStyle("Fusion")

    try:
        config_path = find_launcher_config()
        config = load_launcher_config(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        QMessageBox.critical(None, "启动失败", str(error))
        return 1

    window = BackfillLauncherWindow(config_path, config)
    window.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
