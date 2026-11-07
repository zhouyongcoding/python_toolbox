﻿import sys
import hashlib
import html
import os
import re
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTextEdit, QFileDialog, QMessageBox, QGroupBox, QComboBox,
    QFormLayout, QDateEdit, QDateTimeEdit, QAbstractSpinBox, QProgressBar, QTabWidget, QFrame,
    QSplitter, QListWidget, QListWidgetItem
)
from PySide6.QtCore import Qt, QDate, QDateTime, QTime, QSettings, QUrl, QMimeData
from PySide6.QtGui import QDesktopServices, QPixmap, QFont, QGuiApplication

from app.config import SERVER_URL
from app.services.async_bridge import run_in_background
from app.ui.image_viewer import DraggableImageView

NG_FOLDER_SETTINGS_KEY = "toolbox/ng_stats/last_folder"
IMAGE_SEARCH_FOLDER_SETTINGS_KEY = "toolbox/image_search/last_folder"
DEFAULT_NG_FOLDER = r"D:\判定结果"
DEFAULT_IMAGE_SEARCH_FOLDER = r"D:\判定结果"
IMAGE_EXTENSIONS = {".avif", ".bmp", ".gif", ".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


Y_PATTERN = re.compile(r"[Y]-(\d+)-(\d+)-?([AB])(?=-|\.|$)", re.IGNORECASE)


def extract_unique_scan_ids(text: str) -> list[str]:
    identifiers = set()
    for match in Y_PATTERN.finditer(text):
        identifiers.add(str(int(match.group(2))))
    text_without_y = Y_PATTERN.sub('', text)
    for match in re.findall(r"(?:[高低侧AB]\s*)?(\d+)", text_without_y):
        identifiers.add(str(int(match)))
    return sorted(identifiers, key=int)


def extract_ng_defect_category_ids(filenames: list[str]) -> dict[tuple[str, str], set[str]]:
    category_ids: dict[tuple[str, str], set[str]] = {}
    for filename in filenames:
        y_match = Y_PATTERN.search(filename)
        if y_match:
            batch = y_match.group(1)
            pid = str(int(y_match.group(2)))
            side = y_match.group(3).upper()
            category_ids.setdefault((batch, side), set()).add(pid)
            continue
        category_match = re.match(r"^\s*(高|低|侧|A|B|C)\s*(?=\d)", filename, re.IGNORECASE)
        identifier_match = re.search(r"(?:[高低侧ABC]\s*)?(\d+)", filename, re.IGNORECASE)
        if category_match and identifier_match:
            category = category_match.group(1).upper()
            category_ids.setdefault(("老格式", category), set()).add(str(int(identifier_match.group(1))))
    return category_ids


def read_filenames_from_folder(folder_path: str) -> list[str]:
    filenames = []

    def raise_walk_error(error):
        raise error

    for _, _, names in os.walk(folder_path, onerror=raise_walk_error):
        filenames.extend(names)
    return filenames


def find_images_by_identifier(folder_path: str, identifier: str) -> list[Path]:
    identifier = identifier.strip()
    if not identifier.isdigit():
        raise ValueError("编号必须是数字")

    normalized_identifier = identifier.lstrip("0") or "0"
    old_pattern = re.compile(
        rf"(?<![A-Za-z0-9])(?:A|B|高|低|侧)?0*{re.escape(normalized_identifier)}(?!\d)",
        re.IGNORECASE,
    )
    y_pattern = re.compile(
        rf"Y-\d+-0*{re.escape(normalized_identifier)}-?[AB](?=-|\.|$)",
        re.IGNORECASE,
    )
    matches = []

    def raise_walk_error(error):
        raise error

    for root, _, filenames in os.walk(folder_path, onerror=raise_walk_error):
        for filename in filenames:
            path = Path(root) / filename
            if path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            stem = path.stem
            if Y_PATTERN.search(stem):
                if y_pattern.search(stem):
                    matches.append(path)
            else:
                if old_pattern.search(stem):
                    matches.append(path)

    return sorted(matches, key=lambda path: str(path).lower())

def create_image_clipboard_data(image_paths: list[Path]) -> QMimeData:
    if not image_paths:
        raise ValueError("没有可复制的图片")
    if any(not image_path.is_file() for image_path in image_paths):
        raise FileNotFoundError("一个或多个图片文件已不存在，请重新查询")

    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(image_path.resolve())) for image_path in image_paths])
    return mime_data


def highlight_identifier_in_filename(filename: str, identifier: str) -> str:
    normalized_identifier = identifier.strip().lstrip("0") or "0"
    pattern = re.compile(
        rf"(?<![A-Za-z0-9])(?:A|B|高|低|侧)?(0*{re.escape(normalized_identifier)})(?!\d)",
        re.IGNORECASE,
    )
    highlights = [match.span(1) for match in pattern.finditer(filename)]
    highlights.extend(
        match.span()
        for match in re.finditer(r"[^\\/]+(?=[\\/])", filename)
        if re.search(r"NG", match.group(), re.IGNORECASE)
    )
    if not highlights:
        return html.escape(filename)

    highlights.sort()
    merged = []
    for start, end in highlights:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    parts = []
    cursor = 0
    for start, end in merged:
        parts.append(html.escape(filename[cursor:start]))
        parts.append(
            f'<span style="color:#e53935;font-weight:600">'
            f"{html.escape(filename[start:end])}</span>"
        )
        cursor = end
    parts.append(html.escape(filename[cursor:]))
    return "".join(parts)


class ToolboxView(QWidget):
    def __init__(self):
        super().__init__()
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("工具箱")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #2f4b7c;")
        layout.addWidget(title)

        desc = QLabel("常用工具集合")
        desc.setStyleSheet("color: #888; font-size: 12px; margin-bottom: 8px;")
        layout.addWidget(desc)

        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #ddd; background: white; border-radius: 4px; }"
            "QTabBar::tab { padding: 8px 18px; border: 1px solid #ddd; border-bottom: none; "
            "border-top-left-radius: 4px; border-top-right-radius: 4px; background: #f0f2f5; "
            "margin-right: 2px; }"
            "QTabBar::tab:selected { background: white; font-weight: bold; color: #2f4b7c; }"
        )

        self._build_qr_tab(tabs)
        self._build_hash_tab(tabs)
        self._build_date_tab(tabs)
        self._build_identifier_tab(tabs)
        self._build_image_search_tab(tabs)
        self._build_ng_stats_tab(tabs)

        layout.addWidget(tabs)

    # ------------------------------------------------------------------
    # 二维码解析
    # ------------------------------------------------------------------
    def _build_qr_tab(self, tabs):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(16, 16, 16, 16)
        tab_layout.setSpacing(12)

        hint = QLabel("选择一张包含二维码 / 条形码的图片，自动识别和解码。")
        hint.setStyleSheet("color: #666; font-size: 12px;")
        hint.setWordWrap(True)
        tab_layout.addWidget(hint)

        splitter = QSplitter(Qt.Horizontal)

        left_panel = QVBoxLayout()
        left_panel.setSpacing(8)

        self.qr_preview_label = QLabel("未选择图片")
        self.qr_preview_label.setAlignment(Qt.AlignCenter)
        self.qr_preview_label.setMinimumHeight(250)
        self.qr_preview_label.setStyleSheet(
            "QLabel { background: #f5f5f5; border: 1px dashed #ccc; color: #999; border-radius: 4px; }"
        )
        left_panel.addWidget(self.qr_preview_label)

        btn_row = QHBoxLayout()
        btn_select = QPushButton("选择图片")
        btn_select.setStyleSheet(
            "QPushButton { background-color: #2f4b7c; color: white; padding: 6px 16px; "
            "border-radius: 4px; font-size: 12px; }"
            "QPushButton:hover { background-color: #3d5f99; }"
        )
        btn_select.clicked.connect(self._select_qr_image)
        btn_row.addWidget(btn_select)

        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._clear_qr)
        btn_row.addWidget(btn_clear)
        btn_row.addStretch()
        left_panel.addLayout(btn_row)

        left_widget = QWidget()
        left_widget.setLayout(left_panel)
        splitter.addWidget(left_widget)

        right_panel = QVBoxLayout()
        right_panel.setSpacing(8)

        right_title = QLabel("识别结果")
        right_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        right_panel.addWidget(right_title)

        self.qr_result_text = QTextEdit()
        self.qr_result_text.setReadOnly(True)
        self.qr_result_text.setPlaceholderText("二维码 / 条形码内容将显示在这里...")
        self.qr_result_text.setStyleSheet(
            "QTextEdit { font-size: 14px; border: 1px solid #ddd; border-radius: 4px; padding: 8px; }"
        )
        right_panel.addWidget(self.qr_result_text)

        self.qr_progress = QProgressBar()
        self.qr_progress.setRange(0, 0)
        self.qr_progress.setFixedHeight(4)
        self.qr_progress.setTextVisible(False)
        self.qr_progress.hide()
        right_panel.addWidget(self.qr_progress)

        right_widget = QWidget()
        right_widget.setLayout(right_panel)
        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        tab_layout.addWidget(splitter)

        tabs.addTab(tab, "二维码解析")

    def _select_qr_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择二维码图片", "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)"
        )
        if not file_path:
            return

        pixmap = QPixmap(file_path)
        if pixmap.isNull():
            QMessageBox.warning(self, "错误", "无法加载图片")
            return

        scaled = pixmap.scaled(
            self.qr_preview_label.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )
        self.qr_preview_label.setPixmap(scaled)
        self.qr_preview_label.setStyleSheet(
            "QLabel { background: white; border: 1px solid #ddd; border-radius: 4px; }"
        )

        self.qr_progress.show()
        self.qr_result_text.clear()

        def _scan():
            try:
                with open(file_path, "rb") as f:
                    raw = f.read()
                resp = requests.post(
                    f"{SERVER_URL}/api/scan_qr",
                    files={"file": (Path(file_path).name, raw)},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success"):
                        return data.get("data", "")
                    return f"识别失败: {data.get('message', '未知错误')}"
                return f"请求失败 (HTTP {resp.status_code})"
            except Exception as e:
                return f"异常: {e}"

        def _on_done(result):
            self.qr_progress.hide()
            self.qr_result_text.setPlainText(result or "(未识别到内容)")

        def _on_error(e):
            self.qr_progress.hide()
            self.qr_result_text.setPlainText(f"错误: {e}")

        run_in_background(_scan, _on_done, _on_error)

    def _clear_qr(self):
        self.qr_preview_label.setText("未选择图片")
        self.qr_preview_label.setPixmap(QPixmap())
        self.qr_preview_label.setStyleSheet(
            "QLabel { background: #f5f5f5; border: 1px dashed #ccc; color: #999; border-radius: 4px; }"
        )
        self.qr_result_text.clear()

    # ------------------------------------------------------------------
    # 文件哈希
    # ------------------------------------------------------------------
    def _build_hash_tab(self, tabs):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(16, 16, 16, 16)
        tab_layout.setSpacing(12)

        hint = QLabel("计算文件的 MD5 / SHA256 哈希值")
        hint.setStyleSheet("color: #666; font-size: 12px;")
        tab_layout.addWidget(hint)

        file_frame = QHBoxLayout()
        file_frame.setSpacing(8)

        self.hash_file_edit = QLineEdit()
        self.hash_file_edit.setPlaceholderText("选择文件...")
        self.hash_file_edit.setReadOnly(True)
        self.hash_file_edit.setStyleSheet(
            "QLineEdit { padding: 6px; border: 1px solid #ddd; border-radius: 4px; }"
        )
        file_frame.addWidget(self.hash_file_edit)

        btn_browse = QPushButton("浏览")
        btn_browse.clicked.connect(self._select_hash_file)
        file_frame.addWidget(btn_browse)

        btn_calc = QPushButton("计算")
        btn_calc.setStyleSheet(
            "QPushButton { background-color: #2f4b7c; color: white; padding: 6px 16px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #3d5f99; }"
        )
        btn_calc.clicked.connect(self._calc_hash)
        file_frame.addWidget(btn_calc)

        tab_layout.addLayout(file_frame)

        group = QGroupBox("哈希结果")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #ddd; border-radius: 4px; "
            "padding-top: 14px; margin-top: 8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }"
        )
        form = QFormLayout(group)
        form.setSpacing(10)

        self.md5_edit = QLineEdit()
        self.md5_edit.setReadOnly(True)
        self.md5_edit.setStyleSheet("QLineEdit { font-family: Consolas, monospace; font-size: 13px; padding: 6px; "
                                    "border: 1px solid #ddd; border-radius: 4px; background: #fafafa; }")
        form.addRow("MD5:", self.md5_edit)

        self.sha256_edit = QLineEdit()
        self.sha256_edit.setReadOnly(True)
        self.sha256_edit.setStyleSheet("QLineEdit { font-family: Consolas, monospace; font-size: 13px; padding: 6px; "
                                       "border: 1px solid #ddd; border-radius: 4px; background: #fafafa; }")
        form.addRow("SHA256:", self.sha256_edit)

        self.hash_progress = QProgressBar()
        self.hash_progress.setRange(0, 0)
        self.hash_progress.setFixedHeight(4)
        self.hash_progress.setTextVisible(False)
        self.hash_progress.hide()
        form.addRow(self.hash_progress)

        tab_layout.addWidget(group)
        tab_layout.addStretch()

        tabs.addTab(tab, "文件哈希")

    def _select_hash_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "选择文件")
        if file_path:
            self.hash_file_edit.setText(file_path)
            self.md5_edit.clear()
            self.sha256_edit.clear()

    def _calc_hash(self):
        file_path = self.hash_file_edit.text().strip()
        if not file_path:
            QMessageBox.warning(self, "提示", "请先选择文件")
            return
        try:
            p = Path(file_path)
            if not p.exists():
                QMessageBox.warning(self, "提示", "文件不存在")
                return
        except Exception:
            QMessageBox.warning(self, "提示", "无效的文件路径")
            return

        self.hash_progress.show()

        def _do_hash():
            md5 = hashlib.md5()
            sha256 = hashlib.sha256()
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    md5.update(chunk)
                    sha256.update(chunk)
            return md5.hexdigest(), sha256.hexdigest()

        def _on_done(result):
            self.hash_progress.hide()
            md5_val, sha256_val = result
            self.md5_edit.setText(md5_val)
            self.sha256_edit.setText(sha256_val)

        def _on_error(e):
            self.hash_progress.hide()
            QMessageBox.critical(self, "错误", f"计算哈希失败: {e}")

        run_in_background(_do_hash, _on_done, _on_error)

    # ------------------------------------------------------------------
    # 日期计算
    # ------------------------------------------------------------------
    def _build_date_tab(self, tabs):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(16, 16, 16, 16)
        tab_layout.setSpacing(12)

        hint = QLabel("计算两个日期之间的间隔天数，或推算 N 天后的日期。")
        hint.setStyleSheet("color: #666; font-size: 12px;")
        tab_layout.addWidget(hint)

        mode_layout = QHBoxLayout()
        mode_layout.setSpacing(12)

        self.diff_btn = QPushButton("日期差计算")
        self.diff_btn.setCheckable(True)
        self.diff_btn.setChecked(True)
        self.diff_btn.setStyleSheet(
            "QPushButton { padding: 6px 16px; border: 1px solid #ddd; border-radius: 4px; "
            "background: #f5f5f5; }"
            "QPushButton:checked { background: #2f4b7c; color: white; font-weight: bold; }"
        )
        self.diff_btn.clicked.connect(lambda: self._switch_date_mode("diff"))
        mode_layout.addWidget(self.diff_btn)

        self.add_btn = QPushButton("日期推算")
        self.add_btn.setCheckable(True)
        self.add_btn.setStyleSheet(
            "QPushButton { padding: 6px 16px; border: 1px solid #ddd; border-radius: 4px; "
            "background: #f5f5f5; }"
            "QPushButton:checked { background: #2f4b7c; color: white; font-weight: bold; }"
        )
        self.add_btn.clicked.connect(lambda: self._switch_date_mode("add"))
        mode_layout.addWidget(self.add_btn)
        mode_layout.addStretch()
        tab_layout.addLayout(mode_layout)

        self.date_stack = QVBoxLayout()
        tab_layout.addLayout(self.date_stack)

        self._build_date_diff_panel()
        self._build_date_add_panel()

        self.date_result_label = QLabel("")
        self.date_result_label.setStyleSheet(
            "QLabel { font-size: 14px; color: #2f4b7c; font-weight: bold; "
            "padding: 10px; border: 1px solid #c8d6e5; border-radius: 4px; "
            "background: #e8f0fe; }"
        )
        self.date_result_label.setWordWrap(True)
        tab_layout.addWidget(self.date_result_label)

        tabs.addTab(tab, "日期计算")

    def _switch_date_mode(self, mode):
        self.diff_btn.setChecked(mode == "diff")
        self.add_btn.setChecked(mode == "add")
        self.diff_panel.setVisible(mode == "diff")
        self.add_panel.setVisible(mode == "add")
        self.date_result_label.setText("")

    def _build_date_diff_panel(self):
        self.diff_panel = QFrame()
        diff_layout = QHBoxLayout(self.diff_panel)
        diff_layout.setContentsMargins(0, 0, 0, 0)
        diff_layout.setSpacing(8)

        diff_layout.addWidget(QLabel("开始日期:"))
        self.date_start = QDateEdit()
        self.date_start.setCalendarPopup(True)
        self.date_start.setDate(QDate.currentDate())
        self.date_start.setStyleSheet(
            "QDateEdit { padding: 6px; border: 1px solid #ddd; border-radius: 4px; }"
        )
        diff_layout.addWidget(self.date_start)

        diff_layout.addWidget(QLabel("结束日期:"))
        self.date_end = QDateEdit()
        self.date_end.setCalendarPopup(True)
        self.date_end.setDate(QDate.currentDate())
        self.date_end.setStyleSheet(
            "QDateEdit { padding: 6px; border: 1px solid #ddd; border-radius: 4px; }"
        )
        diff_layout.addWidget(self.date_end)

        btn_calc_diff = QPushButton("计算间隔")
        btn_calc_diff.setStyleSheet(
            "QPushButton { background-color: #2f4b7c; color: white; padding: 6px 16px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #3d5f99; }"
        )
        btn_calc_diff.clicked.connect(self._calc_date_diff)
        diff_layout.addWidget(btn_calc_diff)
        diff_layout.addStretch()

        self.date_stack.addWidget(self.diff_panel)

    def _build_date_add_panel(self):
        self.add_panel = QFrame()
        self.add_panel.setVisible(False)
        add_layout = QHBoxLayout(self.add_panel)
        add_layout.setContentsMargins(0, 0, 0, 0)
        add_layout.setSpacing(8)

        add_layout.addWidget(QLabel("起始日期:"))
        self.date_base = QDateEdit()
        self.date_base.setCalendarPopup(True)
        self.date_base.setDate(QDate.currentDate())
        self.date_base.setStyleSheet(
            "QDateEdit { padding: 6px; border: 1px solid #ddd; border-radius: 4px; }"
        )
        add_layout.addWidget(self.date_base)

        add_layout.addWidget(QLabel("加/减"))
        self.date_offset = QLineEdit()
        self.date_offset.setPlaceholderText("±天数 (如 30 或 -7)")
        self.date_offset.setFixedWidth(100)
        self.date_offset.setStyleSheet(
            "QLineEdit { padding: 6px; border: 1px solid #ddd; border-radius: 4px; }"
        )
        add_layout.addWidget(self.date_offset)

        add_layout.addWidget(QLabel("天"))

        btn_calc_add = QPushButton("推算")
        btn_calc_add.setStyleSheet(
            "QPushButton { background-color: #2f4b7c; color: white; padding: 6px 16px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #3d5f99; }"
        )
        btn_calc_add.clicked.connect(self._calc_date_add)
        add_layout.addWidget(btn_calc_add)
        add_layout.addStretch()

        self.date_stack.addWidget(self.add_panel)

    def _calc_date_diff(self):
        start = self.date_start.date().toPython()
        end = self.date_end.date().toPython()
        delta = (end - start).days
        if delta >= 0:
            self.date_result_label.setText(
                f"从 {start} 到 {end}，共 {delta} 天（包含头尾则 {delta + 1} 天）"
            )
        else:
            self.date_result_label.setText(
                f"从 {start} 到 {end}，相隔 {-delta} 天（结束日期早于开始日期）"
            )

    def _calc_date_add(self):
        base = self.date_base.date().toPython()
        offset_text = self.date_offset.text().strip()
        try:
            offset = int(offset_text)
        except ValueError:
            QMessageBox.warning(self, "提示", "请输入有效的整数天数（如 30 或 -7）")
            return

        result_date = base + dt.timedelta(days=offset)
        self.date_result_label.setText(
            f"{base} {'+' if offset >= 0 else ''}{offset} 天 = {result_date}"
        )

    # ------------------------------------------------------------------
    # C扫编号去重
    # ------------------------------------------------------------------
    def _build_identifier_tab(self, tabs):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(16, 16, 16, 16)
        tab_layout.setSpacing(10)

        hint = QLabel(
            "粘贴图片名称或文件名，自动提取编号并去重。"
            "支持格式：高1、低1、侧1、A1、B1 或 Y-26100002-027-A（编号027）。"
            "支持多行或直接粘贴一组名称。"
        )
        hint.setStyleSheet("color: #666; font-size: 12px;")
        hint.setWordWrap(True)
        tab_layout.addWidget(hint)

        self.identifier_input = QTextEdit()
        self.identifier_input.setPlaceholderText(
            "例如：\n高1-20261021-1-mark.png\nY-26100002-027-A.png\n低1-20261021-2-mark.png"
        )
        self.identifier_input.setStyleSheet(
            "QTextEdit { font-size: 13px; border: 1px solid #ddd; border-radius: 4px; padding: 8px; }"
        )
        tab_layout.addWidget(self.identifier_input, 2)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        analyze_button = QPushButton("统计去重编号")
        analyze_button.setStyleSheet(
            "QPushButton { background-color: #2f4b7c; color: white; padding: 6px 16px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #3d5f99; }"
        )
        analyze_button.clicked.connect(self._analyze_scan_ids)
        button_row.addWidget(analyze_button)

        folder_button = QPushButton("选择文件夹并统计")
        folder_button.clicked.connect(self._analyze_scan_ids_from_folder)
        button_row.addWidget(folder_button)

        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self._clear_scan_ids)
        button_row.addWidget(clear_button)
        button_row.addStretch()
        tab_layout.addLayout(button_row)

        self.identifier_result = QTextEdit()
        self.identifier_result.setReadOnly(True)
        self.identifier_result.setPlaceholderText("去重后的编号和数量将显示在这里...")
        self.identifier_result.setStyleSheet(
            "QTextEdit { font-size: 14px; border: 1px solid #ddd; border-radius: 4px; padding: 8px; }"
        )
        tab_layout.addWidget(self.identifier_result, 1)

        tabs.addTab(tab, "编号去重")

    def _analyze_scan_ids(self, file_count=None):
        identifiers = extract_unique_scan_ids(self.identifier_input.toPlainText())
        if not identifiers:
            result = "未识别到编号，请确认名称中包含高、低或侧及其编号。"
            if file_count is not None:
                result = f"扫描文件数：{file_count}\n{result}"
            self.identifier_result.setPlainText(result)
            return
        result_lines = []
        if file_count is not None:
            result_lines.append(f"扫描文件数：{file_count}")
        result_lines.append(f"去重后共 {len(identifiers)} 个编号：")
        result_lines.extend(identifiers)
        self.identifier_result.setPlainText("\n".join(result_lines))

    def _analyze_scan_ids_from_folder(self):
        folder_path = QFileDialog.getExistingDirectory(self, "选择包含图片的文件夹")
        if not folder_path:
            return
        try:
            filenames = read_filenames_from_folder(folder_path)
        except OSError as exc:
            QMessageBox.critical(self, "读取失败", f"读取文件夹失败：{exc}")
            return

        self.identifier_input.setPlainText("\n".join(filenames))
        self._analyze_scan_ids(file_count=len(filenames))

    def _clear_scan_ids(self):
        self.identifier_input.clear()
        self.identifier_result.clear()

    # ------------------------------------------------------------------
    # 图片编号查询
    # ------------------------------------------------------------------
    def _build_image_search_tab(self, tabs):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(16, 16, 16, 16)
        tab_layout.setSpacing(10)

        hint = QLabel(
            "选择图片文件夹并输入编号，递归查找文件名中的编号，"
            "支持格式：A10055、B10055、高10055、低10055、侧10055 或 Y-26100002-027-A。"
        )
        hint.setStyleSheet("color: #666; font-size: 12px;")
        hint.setWordWrap(True)
        tab_layout.addWidget(hint)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        self.image_search_folder_button = QPushButton("选择图片文件夹")
        self.image_search_folder_button.clicked.connect(self._select_image_search_folder)
        folder_row.addWidget(self.image_search_folder_button)

        self.image_search_folder_label = QLabel("尚未选择文件夹")
        self.image_search_folder_label.setStyleSheet("color: #606266; font-size: 12px;")
        self.image_search_folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        folder_row.addWidget(self.image_search_folder_label, 1)
        tab_layout.addLayout(folder_row)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.image_search_number = QLineEdit()
        self.image_search_number.setPlaceholderText("请输入编号，例如 10055")
        self.image_search_number.returnPressed.connect(self._search_images)
        search_row.addWidget(self.image_search_number)

        search_button = QPushButton("查询图片")
        search_button.setStyleSheet(
            "QPushButton { background-color: #2f4b7c; color: white; padding: 6px 16px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #3d5f99; }"
        )
        search_button.clicked.connect(self._search_images)
        search_row.addWidget(search_button)

        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self._clear_image_search)
        search_row.addWidget(clear_button)

        self.copy_images_button = QPushButton("复制全部图片")
        self.copy_images_button.setEnabled(False)
        self.copy_images_button.clicked.connect(self._copy_all_search_images)
        search_row.addWidget(self.copy_images_button)
        tab_layout.addLayout(search_row)

        self.image_search_result_count = QLabel("")
        self.image_search_result_count.setStyleSheet("color: #606266; font-size: 12px;")
        tab_layout.addWidget(self.image_search_result_count)

        results_splitter = QSplitter(Qt.Horizontal)
        self.image_search_results = QListWidget()
        self.image_search_results.setMinimumWidth(480)
        self.image_search_results.currentItemChanged.connect(self._show_selected_search_image)
        self.image_search_results.itemDoubleClicked.connect(self._open_selected_search_image)
        results_splitter.addWidget(self.image_search_results)

        self.image_search_preview = DraggableImageView()
        self.image_search_preview.setText("查询结果图片会在这里预览")
        results_splitter.addWidget(self.image_search_preview)
        results_splitter.setStretchFactor(0, 1)
        results_splitter.setStretchFactor(1, 2)
        results_splitter.setSizes([600, 900])
        tab_layout.addWidget(results_splitter, 1)

        settings = QSettings("ProductionManagement", "Client")
        previous_path = settings.value(IMAGE_SEARCH_FOLDER_SETTINGS_KEY, "", type=str)
        if previous_path and os.path.isdir(previous_path):
            self.image_search_folder = previous_path
        elif os.path.isdir(DEFAULT_IMAGE_SEARCH_FOLDER):
            self.image_search_folder = DEFAULT_IMAGE_SEARCH_FOLDER
        else:
            self.image_search_folder = ""
        if self.image_search_folder:
            self.image_search_folder_label.setText(self.image_search_folder)

        tabs.addTab(tab, "图片编号查询")

    def _select_image_search_folder(self):
        settings = QSettings("ProductionManagement", "Client")
        previous_path = settings.value(IMAGE_SEARCH_FOLDER_SETTINGS_KEY, "", type=str)
        if previous_path and os.path.isdir(previous_path):
            initial_path = previous_path
        else:
            initial_path = DEFAULT_IMAGE_SEARCH_FOLDER
        folder_path = QFileDialog.getExistingDirectory(self, "选择图片文件夹", initial_path)
        if not folder_path:
            return
        self.image_search_folder = folder_path
        settings.setValue(IMAGE_SEARCH_FOLDER_SETTINGS_KEY, folder_path)
        self.image_search_folder_label.setText(folder_path)
        self.image_search_results.clear()
        self.copy_images_button.setEnabled(False)
        self.image_search_result_count.clear()
        self.image_search_preview.setText("输入编号后查询图片")

    def _search_images(self):
        folder_path = getattr(self, "image_search_folder", "")
        identifier = self.image_search_number.text().strip()
        if not folder_path:
            QMessageBox.warning(self, "提示", "请先选择图片文件夹")
            return
        if not identifier.isdigit():
            QMessageBox.warning(self, "提示", "请输入有效的数字编号")
            return

        try:
            matches = find_images_by_identifier(folder_path, identifier)
        except OSError as exc:
            QMessageBox.critical(self, "读取失败", f"扫描图片文件夹失败：{exc}")
            return

        self.image_search_results.clear()
        self.image_search_preview.setText("请从左侧选择图片预览")
        self.image_search_result_count.setText(f"找到 {len(matches)} 张匹配图片")
        self.copy_images_button.setEnabled(bool(matches))
        for path in matches:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, str(path))
            item.setToolTip(str(path))
            self.image_search_results.addItem(item)
            label = QLabel(highlight_identifier_in_filename(str(path), identifier))
            label.setTextFormat(Qt.RichText)
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            label.setMinimumWidth(460)
            label.setStyleSheet(
                "QLabel { padding: 6px; background: transparent; font-size: 12px; }"
            )
            item.setSizeHint(label.sizeHint())
            self.image_search_results.setItemWidget(item, label)

    def _copy_all_search_images(self):
        image_paths = [
            Path(self.image_search_results.item(index).data(Qt.UserRole))
            for index in range(self.image_search_results.count())
        ]
        if not image_paths:
            QMessageBox.warning(self, "提示", "当前没有可复制的图片")
            return

        try:
            mime_data = create_image_clipboard_data(image_paths)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法复制", str(exc))
            return

        QGuiApplication.clipboard().setMimeData(mime_data)
        QMessageBox.information(
            self,
            "复制完成",
            f"已将 {len(image_paths)} 张图片复制到剪贴板，可切换到微信聊天窗口按 Ctrl+V 粘贴。",
        )

    def _show_selected_search_image(self, current, previous=None):
        previous_label = self.image_search_results.itemWidget(previous) if previous else None
        if previous_label:
            previous_label.setStyleSheet("QLabel { padding: 6px; background: transparent; }")
        if current is None:
            return
        current_label = self.image_search_results.itemWidget(current)
        if current_label:
            current_label.setStyleSheet("QLabel { padding: 6px; background: #e8f0fe; }")
        image_path = current.data(Qt.UserRole)
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            self.image_search_preview.setText(f"无法预览图片：{image_path}")
            return
        self.image_search_preview.setPixmap(pixmap)

    def _open_selected_search_image(self, item, _column=0):
        image_path = item.data(Qt.UserRole)
        if not image_path or not QDesktopServices.openUrl(QUrl.fromLocalFile(image_path)):
            QMessageBox.warning(self, "打开失败", "无法使用系统默认图片查看器打开该图片。")

    def _clear_image_search(self):
        self.image_search_folder = ""
        self.image_search_folder_label.setText("尚未选择文件夹")
        self.image_search_number.clear()
        self.image_search_results.clear()
        self.copy_images_button.setEnabled(False)
        self.image_search_result_count.clear()
        self.image_search_preview.setText("查询结果图片会在这里预览")

    # ------------------------------------------------------------------
    # 良品 / NG 统计
    # ------------------------------------------------------------------
    def _build_ng_stats_tab(self, tabs):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(16, 16, 16, 16)
        tab_layout.setSpacing(10)

        hint = QLabel(
            "选择需统计文件夹：根目录直接存放良品图，子文件夹NG存放NG图。\n"
            "支持同时选择多个文件夹合并统计；同一批次编号跨文件夹会自动合并。\n"
            "支持文件名格式：高1、低1、侧1、A1、B1 或 Y-26100002-027-A（编号027，A面）。"
        )
        hint.setStyleSheet("color: #666; font-size: 12px;")
        hint.setWordWrap(True)
        tab_layout.addWidget(hint)

        self._ng_dt_start = None
        self._ng_dt_end = None

        range_row = QHBoxLayout()
        range_row.setSpacing(8)

        range_row.addWidget(QLabel("编号区间："))
        self.range_start = QLineEdit()
        self.range_start.setPlaceholderText("起始编号")
        self.range_start.setFixedWidth(80)
        self.range_start.setStyleSheet("QLineEdit { padding: 4px; border: 1px solid #ddd; border-radius: 4px; }")
        range_row.addWidget(self.range_start)

        range_row.addWidget(QLabel("-"))

        self.range_end = QLineEdit()
        self.range_end.setPlaceholderText("结束编号")
        self.range_end.setFixedWidth(80)
        self.range_end.setStyleSheet("QLineEdit { padding: 4px; border: 1px solid #ddd; border-radius: 4px; }")
        range_row.addWidget(self.range_end)

        range_row.addWidget(QLabel("（留空则统计全部）"))
        range_row.addStretch()
        tab_layout.addLayout(range_row)

        date_row = QHBoxLayout()
        date_row.setSpacing(8)
        date_row.addWidget(QLabel("时间区间："))

        self.date_start_btn = QPushButton("选起始")
        self.date_start_btn.setFixedWidth(72)
        self.date_start_btn.setStyleSheet(
            "QPushButton { padding: 4px 8px; border: 1px solid #4a90d9; border-radius: 4px; background: #fff; color: #1a56db; }"
            "QPushButton:hover { background: #f0f4ff; }"
        )
        self.date_start_btn.clicked.connect(lambda: self._pick_image_ctime("start"))
        date_row.addWidget(self.date_start_btn)

        self.date_start_label = QLabel("不限")
        self.date_start_label.setStyleSheet("color: #666; min-width: 130px;")
        date_row.addWidget(self.date_start_label)

        date_row.addWidget(QLabel("—"))

        self.date_end_btn = QPushButton("选结束")
        self.date_end_btn.setFixedWidth(72)
        self.date_end_btn.setStyleSheet(
            "QPushButton { padding: 4px 8px; border: 1px solid #e53e3e; border-radius: 4px; background: #fff; color: #c53030; }"
            "QPushButton:hover { background: #fff5f5; }"
        )
        self.date_end_btn.clicked.connect(lambda: self._pick_image_ctime("end"))
        date_row.addWidget(self.date_end_btn)

        self.date_end_label = QLabel("不限")
        self.date_end_label.setStyleSheet("color: #666; min-width: 130px;")
        date_row.addWidget(self.date_end_label)

        clear_date_btn = QPushButton("清空")
        clear_date_btn.setFixedWidth(56)
        clear_date_btn.setStyleSheet(
            "QPushButton { padding: 4px 8px; border: 1px solid #ddd; border-radius: 4px; background: #fafafa; }"
            "QPushButton:hover { background: #f0f0f0; }"
        )
        clear_date_btn.clicked.connect(self._clear_ng_date_filters)
        date_row.addWidget(clear_date_btn)

        date_row.addStretch()
        tab_layout.addLayout(date_row)

        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel("批次筛选："))
        self.batch_filter = QComboBox()
        self.batch_filter.addItem("全部批次", "")
        self.batch_filter.setMinimumWidth(180)
        self.batch_filter.setStyleSheet(
            "QComboBox { padding: 4px; border: 1px solid #ddd; border-radius: 4px; }"
        )
        batch_row.addWidget(self.batch_filter)
        batch_row.addWidget(QLabel("（默认统计所有批次）"))
        batch_row.addStretch()
        tab_layout.addLayout(batch_row)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)

        self.add_folder_btn = QPushButton("添加文件夹")
        self.add_folder_btn.setStyleSheet(
            "QPushButton { background-color: #2f4b7c; color: white; padding: 6px 14px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #3d5f99; }"
        )
        self.add_folder_btn.clicked.connect(self._add_ng_folder)
        folder_row.addWidget(self.add_folder_btn)

        self.remove_folder_btn = QPushButton("移除选中")
        self.remove_folder_btn.setStyleSheet(
            "QPushButton { background-color: #8c8c8c; color: white; padding: 6px 14px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #a8a8a8; }"
        )
        self.remove_folder_btn.clicked.connect(self._remove_ng_folder)
        folder_row.addWidget(self.remove_folder_btn)

        self.clear_folders_btn = QPushButton("清空列表")
        self.clear_folders_btn.setStyleSheet(
            "QPushButton { background-color: #c62828; color: white; padding: 6px 14px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #d32f2f; }"
        )
        self.clear_folders_btn.clicked.connect(self._clear_ng_folders)
        folder_row.addWidget(self.clear_folders_btn)
        folder_row.addStretch()
        tab_layout.addLayout(folder_row)

        self.ng_folder_list = QListWidget()
        self.ng_folder_list.setFixedHeight(100)
        self.ng_folder_list.setStyleSheet(
            "QListWidget { border: 1px solid #ddd; border-radius: 4px; padding: 4px; font-size: 12px; }"
            "QListWidget::item { padding: 3px 6px; }"
            "QListWidget::item:selected { background-color: #e3f2fd; color: #1a56db; }"
        )
        tab_layout.addWidget(self.ng_folder_list)

        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(8)

        self.stats_btn = QPushButton("统计")
        self.stats_btn.setStyleSheet(
            "QPushButton { background-color: #388e3c; color: white; padding: 6px 16px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #4caf50; }"
        )
        self.stats_btn.setEnabled(False)
        self.stats_btn.clicked.connect(self._analyze_ng_stats)
        btn_row2.addWidget(self.stats_btn)

        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self._clear_ng_stats)
        btn_row2.addWidget(clear_btn)
        btn_row2.addStretch()
        tab_layout.addLayout(btn_row2)

        self.ng_warning_label = QLabel("")
        self.ng_warning_label.setWordWrap(True)
        self.ng_warning_label.setStyleSheet(
            "QLabel { color: #bf360c; background: #fff8e1; border: 1px solid #ffcc80; "
            "border-radius: 4px; padding: 8px 12px; font-size: 12px; line-height: 1.6; }"
        )
        self.ng_warning_label.hide()
        tab_layout.addWidget(self.ng_warning_label)

        self.ng_detail_table = QTextEdit()
        self.ng_detail_table.setReadOnly(True)
        self.ng_detail_table.setPlaceholderText("统计结果将显示在这里...")
        self.ng_detail_table.setStyleSheet(
            "QTextEdit { font-size: 14px; border: 1px solid #ddd; border-radius: 4px; padding: 8px; }"
        )
        tab_layout.addWidget(self.ng_detail_table, 3)

        tabs.addTab(tab, "良品/NG统计")
        self._load_saved_ng_folders()

    def _load_saved_ng_folders(self):
        settings = QSettings("ProductionManagement", "Client")
        saved = settings.value(NG_FOLDER_SETTINGS_KEY, "", type=str)
        if saved:
            paths = [p.strip() for p in saved.split(";") if p.strip() and os.path.isdir(p.strip())]
            for p in paths:
                self.ng_folder_list.addItem(p)
            if paths:
                self.stats_btn.setEnabled(True)

    def _add_ng_folder(self):
        settings = QSettings("ProductionManagement", "Client")
        previous_path = settings.value(NG_FOLDER_SETTINGS_KEY, "", type=str)
        if previous_path:
            first_saved = previous_path.split(";")[0].strip()
            initial_path = first_saved if os.path.isdir(first_saved) else (
                DEFAULT_NG_FOLDER if os.path.isdir(DEFAULT_NG_FOLDER)
                else os.path.splitdrive(DEFAULT_NG_FOLDER)[0] + os.sep
            )
        elif os.path.isdir(DEFAULT_NG_FOLDER):
            initial_path = DEFAULT_NG_FOLDER
        else:
            initial_path = os.path.splitdrive(DEFAULT_NG_FOLDER)[0] + os.sep

        selected_path = QFileDialog.getExistingDirectory(
            self,
            "选择需统计文件夹（根目录存良品图，NG子文件夹存NG图）",
            initial_path,
        )
        if not selected_path:
            return

        existing = [self.ng_folder_list.item(i).text() for i in range(self.ng_folder_list.count())]
        if selected_path in existing:
            QMessageBox.information(self, "提示", f"该文件夹已在列表中：\n{selected_path}")
            return

        self.ng_folder_list.addItem(selected_path)
        self.stats_btn.setEnabled(True)
        self._reset_ng_batch_filter()

        all_paths = [self.ng_folder_list.item(i).text() for i in range(self.ng_folder_list.count())]
        settings.setValue(NG_FOLDER_SETTINGS_KEY, ";".join(all_paths))

    def _remove_ng_folder(self):
        current = self.ng_folder_list.currentRow()
        if current < 0:
            QMessageBox.information(self, "提示", "请先选中要移除的文件夹")
            return
        self.ng_folder_list.takeItem(current)
        self.stats_btn.setEnabled(self.ng_folder_list.count() > 0)
        self._reset_ng_batch_filter()

        settings = QSettings("ProductionManagement", "Client")
        all_paths = [self.ng_folder_list.item(i).text() for i in range(self.ng_folder_list.count())]
        settings.setValue(NG_FOLDER_SETTINGS_KEY, ";".join(all_paths))

    def _clear_ng_folders(self):
        self.ng_folder_list.clear()
        self.stats_btn.setEnabled(False)
        self._reset_ng_batch_filter()
        settings = QSettings("ProductionManagement", "Client")
        settings.setValue(NG_FOLDER_SETTINGS_KEY, "")

    def _analyze_ng_stats(self):
        all_parent_paths = [self.ng_folder_list.item(i).text() for i in range(self.ng_folder_list.count())]
        if not all_parent_paths:
            QMessageBox.warning(self, "提示", "请先添加需统计的文件夹")
            return

        range_start_text = self.range_start.text().strip()
        range_end_text = self.range_end.text().strip()
        range_start = int(range_start_text) if range_start_text else None
        range_end = int(range_end_text) if range_end_text else None

        ok_ids_by_batch: dict[str, set[str]] = {}
        ng_ids_by_batch: dict[str, set[str]] = {}
        batch_ctimes: dict[str, list[float]] = {}
        ng_defect_filenames = []
        stray_ng_filenames: list[tuple[str, str]] = []
        invalid_entries: list[tuple[str, str]] = []

        try:
            for parent_path in all_parent_paths:
                for root, dirs, files in os.walk(parent_path):
                    rel_root = os.path.relpath(root, parent_path)
                    is_ng_folder = (rel_root != ".") and ("NG" in rel_root.upper())
                    for name in files:
                        full_path = os.path.join(root, name)
                        name_without_ext = os.path.splitext(name)[0]
                        filename_says_ng = "NG" in name_without_ext.upper()
                        is_ng = is_ng_folder or filename_says_ng
                        if filename_says_ng and not is_ng_folder:
                            stray_ng_filenames.append((parent_path, full_path))
                        y_match = Y_PATTERN.search(name)
                        if y_match:
                            batch = y_match.group(1)
                            pid = int(y_match.group(2))
                        else:
                            match = re.search(r"(?:[高低侧ABC]\s*)?(\d+)", name, re.IGNORECASE)
                            if not match:
                                if is_ng:
                                    invalid_entries.append((parent_path, full_path))
                                continue
                            pid = int(match.group(1))
                            batch = "老格式"
                        if range_start is not None and pid < range_start:
                            continue
                        if range_end is not None and pid > range_end:
                            continue
                        pid_str = str(pid)
                        ftime = None
                        try:
                            ftime = os.path.getmtime(full_path)
                            batch_ctimes.setdefault(batch, []).append(ftime)
                        except OSError:
                            pass
                        if self._ng_dt_start is not None or self._ng_dt_end is not None:
                            if ftime is None:
                                continue
                            file_dt = QDateTime.fromSecsSinceEpoch(int(ftime))
                            if self._ng_dt_start is not None and file_dt < self._ng_dt_start:
                                continue
                            if self._ng_dt_end is not None and file_dt >= self._ng_dt_end:
                                continue
                        if is_ng:
                            ng_ids_by_batch.setdefault(batch, set()).add(pid_str)
                            ng_defect_filenames.append(name)
                        else:
                            ok_ids_by_batch.setdefault(batch, set()).add(pid_str)
        except OSError as exc:
            QMessageBox.critical(self, "读取失败", f"读取文件夹失败：{exc}")
            return

        available_batches = sorted(set(ok_ids_by_batch.keys()) | set(ng_ids_by_batch.keys()))
        selected_batch = self.batch_filter.currentData()
        if selected_batch not in available_batches:
            selected_batch = ""

        self.batch_filter.blockSignals(True)
        self.batch_filter.clear()
        self.batch_filter.addItem("全部批次", "")
        for batch in available_batches:
            self.batch_filter.addItem(batch, batch)
        self.batch_filter.setCurrentIndex(
            self.batch_filter.findData(selected_batch)
        )
        self.batch_filter.blockSignals(False)

        batches = (
            [selected_batch]
            if selected_batch
            else available_batches
        )
        batch_stats = {}
        final_ok_all = set()
        final_ng_all = set()
        for batch in batches:
            ok_set = ok_ids_by_batch.get(batch, set())
            ng_set = ng_ids_by_batch.get(batch, set())
            final_ok = sorted(ok_set - ng_set, key=int)
            final_ng = sorted(ng_set, key=int)
            batch_stats[batch] = (final_ok, final_ng)
            final_ok_all.update(final_ok)
            final_ng_all.update(final_ng)

        total = len(final_ok_all) + len(final_ng_all)
        defect_ids_by_category = extract_ng_defect_category_ids(ng_defect_filenames)
        if selected_batch:
            defect_ids_by_category = {
                key: ids
                for key, ids in defect_ids_by_category.items()
                if key[0] == selected_batch
            }

        table_lines = [
            "<table border='0' cellspacing='12' cellpadding='0' width='100%' "
            "style='table-layout:fixed;margin-left:0;margin-right:auto;'><tr>"
            "<td valign='top' width='540'>"
        ]

        def _fmt_ctime(ts):
            return QDateTime.fromSecsSinceEpoch(int(ts)).toString("yyyy-MM-dd HH:mm")

        if len(batches) > 1:
            all_ctimes: list[float] = []
            for v in batch_ctimes.values():
                all_ctimes.extend(v)
            if all_ctimes:
                t_min, t_max = min(all_ctimes), max(all_ctimes)
                time_range = f"<br><span style='font-size:12px;color:#666;'>创建时间 {_fmt_ctime(t_min)} ~ {_fmt_ctime(t_max)}</span>"
            else:
                time_range = ""
            table_lines.append(
                f"<table style='border-collapse:collapse;width:100%;margin-bottom:8px;background:#fafafa;border:1px solid #e0e0e0;'>"
                f"<tr><td colspan='2' style='height:32px;padding:6px;text-align:center;font-size:14px;font-weight:bold;color:#303133;'>"
                f"汇总　|　总计 {total} 个　|　良品 {len(final_ok_all)}　|　NG {len(final_ng_all)}"
                f"{time_range}</td></tr></table>"
            )

        for batch in batches:
            final_ok, final_ng = batch_stats[batch]
            batch_total = len(final_ok) + len(final_ng)
            max_len = max(len(final_ok), len(final_ng))

            batch_time = ""
            bcts = batch_ctimes.get(batch)
            if bcts:
                t_min, t_max = min(bcts), max(bcts)
                if t_min == t_max:
                    batch_time = f"<br><span style='font-size:12px;color:#666;'>创建时间 {_fmt_ctime(t_min)}</span>"
                else:
                    batch_time = f"<br><span style='font-size:12px;color:#666;'>创建时间 {_fmt_ctime(t_min)} ~ {_fmt_ctime(t_max)}</span>"

            table_lines.append("<table style='border-collapse:collapse;width:100%;margin-bottom:8px;'>")
            table_lines.append(
                f"<tr><td colspan='2' style='height:32px;padding:6px;text-align:center;font-size:14px;font-weight:bold;"
                f"background:#f0f4ff;color:#1a56db;border:1px solid #c7d7f5;'>"
                f"批次 {batch}　|　总计 {batch_total} 个　|　良品 {len(final_ok)}　|　NG {len(final_ng)}"
                f"{batch_time}</td></tr>"
            )
            table_lines.append(
                "<tr>"
                "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#e8f5e9;color:#2e7d32;width:50%;'>良品编号</th>"
                "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#ffebee;color:#c62828;width:50%;'>NG 编号</th>"
                "</tr>"
            )
            for i in range(max_len):
                ok_val = final_ok[i] if i < len(final_ok) else ""
                ng_val = final_ng[i] if i < len(final_ng) else ""
                table_lines.append(
                    f"<tr>"
                    f"<td style='border:1px solid #ccc;padding:6px;text-align:center;color:#2e7d32;font-weight:bold;'>{ok_val}</td>"
                    f"<td style='border:1px solid #ccc;padding:6px;text-align:center;color:#c62828;font-weight:bold;'>{ng_val}</td>"
                    f"</tr>"
                )
            table_lines.append("</table>")

        table_lines.append("</td><td valign='top'>")
        table_lines.append(
            "<table style='border-collapse:collapse;width:100%;margin-left:-80px;'>"
        )
        table_lines.append(
            "<tr><th colspan='3' style='border:1px solid #ccc;padding:8px;text-align:center;"
            "background:#f5f7fa;color:#303133;font-size:15px;'>NG 不良明细</th></tr>"
        )
        table_lines.append(
            "<tr>"
            "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#f5f7fa;'>批次 / 不良类别</th>"
            "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#f5f7fa;'>数量 / 不良率</th>"
            "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#f5f7fa;'>对应 NG 编号</th>"
            "</tr>"
        )
        category_order = ("高", "低", "侧", "A", "B", "C")
        for batch in batches:
            batch_has_rows = False
            for category in category_order:
                category_ids = defect_ids_by_category.get((batch, category))
                if not category_ids:
                    continue
                batch_has_rows = True
                category_count = len(category_ids)
                batch_total = len(batch_stats[batch][0]) + len(batch_stats[batch][1])
                rate = category_count / batch_total * 100 if batch_total else 0
                identifiers = "、".join(sorted(category_ids, key=int))
                table_lines.append(
                    "<tr>"
                    f"<td style='border:1px solid #ccc;padding:6px;text-align:center;font-weight:bold;'>{batch} / {category}</td>"
                    f"<td style='border:1px solid #ccc;padding:6px;text-align:center;color:#c62828;'>"
                    f"{category_count} 个 / {rate:.2f}%</td>"
                    f"<td style='border:1px solid #ccc;padding:6px;text-align:left;color:#c62828;'>{identifiers}</td>"
                    "</tr>"
                )
            if batch_has_rows:
                continue
        if not defect_ids_by_category:
            table_lines.append(
                "<tr><td colspan='3' style='border:1px solid #ccc;padding:8px;text-align:center;color:#909399;'>"
                "未识别到带类别前缀的 NG 编号</td></tr>"
            )
        ng_rate = len(final_ng_all) / total * 100 if total else 0
        table_lines.append(
            "</table>"
            "<table style='border-collapse:collapse;width:100%;margin-top:12px;margin-left:-80px;"
            "background:#fff7f7;border:1px solid #f3c7c7;'>"
            "<tr>"
            "<td style='padding:12px 16px;color:#606266;'>NG 不良率</td>"
            f"<td style='padding:12px 16px;color:#c62828;font-size:20px;font-weight:bold;'>{ng_rate:.2f}%</td>"
            f"<td style='padding:12px 16px;color:#909399;font-size:12px;'>"
            f"NG 数量 {len(final_ng_all)} / 总计产品数 {total}</td>"
            "</tr></table></td></tr></table>"
        )
        warning_parts = []
        if stray_ng_filenames:
            stray_lines = "\n".join(
                f"  {p.replace(pp, '')}" for pp, p in stray_ng_filenames
            )
            warning_parts.append(
                f"⚠ 警告：以下 {len(stray_ng_filenames)} 个文件名含 NG，"
                f"但不在 NG 子文件夹中，统计时已自动归为 NG，请检查是否需要移动：\n{stray_lines}"
            )
        if invalid_entries:
            invalid_lines = "\n".join(
                f"  {p.replace(pp, '')}" for pp, p in invalid_entries
            )
            warning_parts.append(
                f"无法识别的文件（共 {len(invalid_entries)} 个）：\n{invalid_lines}"
            )
        if warning_parts:
            self.ng_warning_label.setText("\n\n".join(warning_parts))
            self.ng_warning_label.show()
        else:
            self.ng_warning_label.hide()
        self.ng_detail_table.setHtml("\n".join(table_lines))

    def _pick_image_ctime(self, which):
        filter_str = (
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp *.gif *.heic *.heif *.avif);;"
            "所有文件 (*.*)"
        )
        path, _ = QFileDialog.getOpenFileName(self, "选择图片以读取修改时间", "", filter_str)
        if not path:
            return
        try:
            ftime = os.path.getmtime(path)
            dt = QDateTime.fromSecsSinceEpoch(int(ftime))
        except OSError as exc:
            QMessageBox.warning(self, "读取失败", f"读取文件时间失败：{exc}")
            return
        text = dt.toString("yyyy-MM-dd HH:mm")
        if which == "start":
            self._ng_dt_start = dt
            self.date_start_label.setText(text)
            self.date_start_label.setStyleSheet("color: #1a56db; font-weight: bold; min-width: 130px;")
        else:
            self._ng_dt_end = dt
            self.date_end_label.setText(text)
            self.date_end_label.setStyleSheet("color: #c53030; font-weight: bold; min-width: 130px;")

    def _clear_ng_date_filters(self):
        self._ng_dt_start = None
        self._ng_dt_end = None
        self.date_start_label.setText("不限")
        self.date_start_label.setStyleSheet("color: #666; min-width: 130px;")
        self.date_end_label.setText("不限")
        self.date_end_label.setStyleSheet("color: #666; min-width: 130px;")

    def _reset_ng_batch_filter(self):
        self.batch_filter.clear()
        self.batch_filter.addItem("全部批次", "")

    def _clear_ng_stats(self):
        self.ng_folder_list.clear()
        self.stats_btn.setEnabled(False)
        self._reset_ng_batch_filter()
        self.range_start.clear()
        self.range_end.clear()
        self._clear_ng_date_filters()
        self.ng_detail_table.clear()
        self.ng_warning_label.clear()
        self.ng_warning_label.hide()
        settings = QSettings("ProductionManagement", "Client")
        settings.setValue(NG_FOLDER_SETTINGS_KEY, "")