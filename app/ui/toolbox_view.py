import sys
import hashlib
import html
import json
import os
import re
import shutil
import uuid
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
    QSplitter, QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QHeaderView, QSpinBox, QCheckBox, QAbstractItemView, QStatusBar, QInputDialog
)
from PySide6.QtCore import Qt, QDate, QDateTime, QTime, QSettings, QUrl, QMimeData
from PySide6.QtGui import QDesktopServices, QPixmap, QFont, QGuiApplication, QColor

from app.config import SERVER_URL
from app.services.async_bridge import run_in_background
from app.ui.image_viewer import DraggableImageView

NG_FOLDER_SETTINGS_KEY = "toolbox/ng_stats/last_folder"
IMAGE_SEARCH_FOLDER_SETTINGS_KEY = "toolbox/image_search/last_folder"
IMAGE_EXPORT_FOLDER_SETTINGS_KEY = "toolbox/image_search/last_export_dir"
TASK_PERSON_SETTINGS_KEY = "toolbox/task/current_person"
DEFAULT_NG_FOLDER = r"D:\判定结果"
DEFAULT_IMAGE_SEARCH_FOLDER = r"D:\判定结果"
IMAGE_EXTENSIONS = {".avif", ".bmp", ".gif", ".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}

TASK_STATUS_TODO = "todo"
TASK_STATUS_DOING = "doing"
TASK_STATUS_DONE = "done"
TASK_STATUS_BLOCKED = "blocked"
TASK_STATUS_LABELS = {
    TASK_STATUS_TODO: "待办",
    TASK_STATUS_DOING: "进行中",
    TASK_STATUS_DONE: "已完成",
    TASK_STATUS_BLOCKED: "已搁置",
}
TASK_STATUS_COLORS = {
    TASK_STATUS_TODO: "#909399",
    TASK_STATUS_DOING: "#e6a23c",
    TASK_STATUS_DONE: "#67c23a",
    TASK_STATUS_BLOCKED: "#f56c6c",
}


def _tasks_data_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(appdata) / "PythonToolbox"
    d.mkdir(parents=True, exist_ok=True)
    return d / "tasks.json"


def _load_tasks_data() -> dict:
    p = _tasks_data_path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_tasks_data(data: dict) -> None:
    p = _tasks_data_path()
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


_SIDE_CHARS = r"[ABC高低侧左右]"
Y_PATTERN = re.compile(
    rf"Y-(\d+)-(?:([ABC高低侧左右])(\d+)|(\d+)(?:-([ABC高低侧左右]))?([ABC高低侧左右])?)(?=-|\.|$)",
    re.IGNORECASE,
)


def _parse_y_match(match) -> tuple[str, str, str]:
    batch = match.group(1)
    if match.group(2):
        side = match.group(2).upper()
        pid = str(int(match.group(3)))
    else:
        pid = str(int(match.group(4)))
        if match.group(5):
            side = match.group(5).upper()
        elif match.group(6):
            side = match.group(6).upper()
        else:
            side = "-"
    return batch, pid, side
T_PATTERN = re.compile(r"^T[-_]+([^-_]+)", re.IGNORECASE)
T_SIDE_NUMBER_PATTERN = re.compile(r"([AB高低侧])\s*(\d+)", re.IGNORECASE)
_DATE8_RE = re.compile(r"20\d{6}")
_SIDE_NUMBER_RE = re.compile(r"([高低侧ABC])\s*(\d+)", re.IGNORECASE)
_STANDALONE_NUMBER_RE = re.compile(r"(?<![A-Za-z\d])(\d{1,5})(?!\d)")
_F_RE = re.compile(r"-R(\d+)(?=[-_]|\.|$)", re.IGNORECASE)
_BATCH_RE = re.compile(r"T[-_](\d{6,8})", re.IGNORECASE)
_SIDE_CHARS_STR = "ABC高低侧左右"
_SIDE_CHARS_SET = set(_SIDE_CHARS_STR)
_RETAKE_SUFFIX_RE = re.compile(r"[-_\s]*R\d+(?=[-_]|$).*$", re.IGNORECASE)


def _clean_category(after_num: str) -> str:
    stripped = _RETAKE_SUFFIX_RE.sub("", after_num).strip("-_ \t")
    if not stripped or stripped[0].upper() != "L":
        return "未分类"
    sep = re.search(r"[-_]", stripped)
    if sep:
        return stripped[:sep.start()].strip()
    return stripped


def _t_identifier_and_category(stem: str) -> tuple[str, str] | None:
    side_num = T_SIDE_NUMBER_PATTERN.search(stem)
    if side_num:
        pid = str(int(side_num.group(2)))
        after_num = stem[side_num.end():]
        return pid, _clean_category(after_num)
    for match in _STANDALONE_NUMBER_RE.finditer(stem):
        if not _DATE8_RE.fullmatch(match.group(1)):
            pid = str(int(match.group(1)))
            after_num = stem[match.end():]
            return pid, _clean_category(after_num)
    return None


def extract_batch_category_from_path(path: Path | str) -> tuple[str, str]:
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    batch_m = _BATCH_RE.search(stem)
    if batch_m:
        batch = batch_m.group(1)
        id_info = _t_identifier_and_category(stem)
        category = id_info[1] if id_info else "未分类"
        return batch, category
    y_m = Y_PATTERN.search(stem)
    if y_m:
        batch = y_m.group(1)
        return batch, "未分类"
    return "老格式", "未分类"


def extract_unique_scan_ids(text: str) -> list[str]:
    identifiers = set()
    for match in Y_PATTERN.finditer(text):
        _, pid, _ = _parse_y_match(match)
        identifiers.add(str(int(pid)))
    text_without_y = Y_PATTERN.sub('', text)
    for match in re.findall(r"(?:[高低侧AB]\s*)?(\d+)", text_without_y):
        identifiers.add(str(int(match)))
    return sorted(identifiers, key=int)


def _extract_side(stem: str) -> str:
    m = T_SIDE_NUMBER_PATTERN.search(stem)
    if m:
        return m.group(1).upper()
    y_m = Y_PATTERN.search(stem)
    if y_m:
        _, _, side = _parse_y_match(y_m)
        if side != "-":
            return side
    old_m = _SIDE_NUMBER_RE.search(stem)
    if old_m:
        return old_m.group(1).upper()
    return "-"


def extract_ng_defect_category_ids(filenames: list[str]) -> dict[str, dict[str, dict[str, set[str]]]]:
    result: dict[str, dict[str, dict[str, set[str]]]] = {}
    for filename in filenames:
        batch = None
        category = "未分类"
        side = "-"
        pid = None

        t_match = T_PATTERN.match(filename)
        if t_match:
            batch = t_match.group(1)
            stem = os.path.splitext(filename)[0]
            id_info = _t_identifier_and_category(stem)
            if not id_info:
                continue
            pid, category = id_info
            side = _extract_side(stem)
        else:
            y_match = Y_PATTERN.search(filename)
            if y_match:
                batch, pid, side = _parse_y_match(y_match)
            else:
                side_num = _SIDE_NUMBER_RE.search(filename)
                if side_num:
                    batch = "老格式"
                    pid = str(int(side_num.group(2)))
                    side = side_num.group(1).upper()
                else:
                    standalone = _STANDALONE_NUMBER_RE.search(filename)
                    if standalone and not _DATE8_RE.fullmatch(standalone.group(1)):
                        batch = "老格式"
                        pid = str(int(standalone.group(1)))

        if not batch or not pid:
            continue
        result.setdefault(batch, {}).setdefault(category, {}).setdefault(side, set()).add(pid)
    return result


def filter_ng_defect_category_ids(
    category_ids: dict[str, dict[str, dict[str, set[str]]]],
    ng_ids_by_batch: dict[str, set[str]],
) -> dict[tuple[str, str, str], set[str]]:
    filtered_ids: dict[tuple[str, str, str], set[str]] = {}
    for batch, cats in category_ids.items():
        batch_ng = ng_ids_by_batch.get(batch, set())
        if not batch_ng:
            continue
        for category, sides in cats.items():
            for side, pids in sides.items():
                matching = pids & batch_ng
                if matching:
                    filtered_ids[(batch, category, side)] = matching
    return filtered_ids


def read_filenames_from_folder(folder_path: str) -> list[str]:
    filenames = []

    def raise_walk_error(error):
        raise error

    for _, _, names in os.walk(folder_path, onerror=raise_walk_error):
        filenames.extend(names)
    return filenames


def scan_batch_category_tree(folder_path: str) -> dict[str, dict[str, list[str]]]:
    tree: dict[str, dict[str, set[str]]] = {}
    try:
        filenames = read_filenames_from_folder(folder_path)
    except OSError:
        return {}
    for filename in filenames:
        stem = os.path.splitext(filename)[0]
        batch: str | None = None
        category: str | None = None
        pid: str | None = None

        batch_m = _BATCH_RE.search(stem)
        if batch_m:
            batch = batch_m.group(1)
            id_info = _t_identifier_and_category(stem)
            if id_info:
                pid, category = id_info
        else:
            y_m = Y_PATTERN.search(stem)
            if y_m:
                batch, pid, _ = _parse_y_match(y_m)
                category = "未分类"

        if not batch or not category or not pid:
            continue
        tree.setdefault(batch, {}).setdefault(category, set()).add(pid)

    result: dict[str, dict[str, list[str]]] = {}
    for batch in sorted(tree.keys()):
        cats = tree[batch]
        result[batch] = {
            cat: sorted(ids, key=int)
            for cat, ids in sorted(cats.items())
        }
    return result


def extract_query_identifier(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    if raw.isdigit():
        return raw
    m = Y_PATTERN.search(raw)
    if m:
        return str(int(m.group(2)))
    nums = re.findall(r"\d+", raw)
    if nums:
        nums.sort(key=len, reverse=True)
        return str(int(nums[0]))
    return None


def _parse_y_search_query(raw: str) -> tuple[str, str] | None:
    match = Y_PATTERN.fullmatch(raw.strip())
    if not match:
        return None
    batch, pid, _ = _parse_y_match(match)
    return batch, pid


def find_images_by_identifier(folder_path: str, identifier: str) -> list[Path]:
    identifier = identifier.strip()
    y_query = _parse_y_search_query(identifier)
    if y_query:
        query_batch, normalized_identifier = y_query
    elif identifier.isdigit():
        query_batch = None
        normalized_identifier = identifier.lstrip("0") or "0"
    else:
        raise ValueError("编号必须是数字")

    old_pattern = re.compile(
        rf"(?<![A-Za-z0-9])(?:{_SIDE_CHARS})?0*{re.escape(normalized_identifier)}(?:{_SIDE_CHARS})?(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    y_pattern = re.compile(
        rf"Y-\d+-(?:{_SIDE_CHARS})?0*{re.escape(normalized_identifier)}(?:-{_SIDE_CHARS})?(?=-|\.|$)",
        re.IGNORECASE,
    )
    t_pattern = re.compile(
        rf"T[-_][^-_]+[-_](?:{_SIDE_CHARS}\s*)?0*{re.escape(normalized_identifier)}(?=[-_]|\.|$)",
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
            if query_batch is not None:
                y_matches = Y_PATTERN.finditer(stem)
                if any(
                    _parse_y_match(match)[0] == query_batch
                    and _parse_y_match(match)[1] == normalized_identifier
                    for match in y_matches
                ):
                    matches.append(path)
                    continue

                batch_match = _BATCH_RE.search(stem)
                id_info = _t_identifier_and_category(stem) if T_PATTERN.match(stem) else None
                if (
                    batch_match
                    and batch_match.group(1) == query_batch
                    and id_info
                    and id_info[0] == normalized_identifier
                ):
                    matches.append(path)
                continue

            if Y_PATTERN.search(stem):
                if y_pattern.search(stem):
                    matches.append(path)
            elif T_PATTERN.match(stem):
                if t_pattern.search(stem):
                    matches.append(path)
            else:
                if old_pattern.search(stem):
                    matches.append(path)

    return sorted(matches, key=lambda path: str(path).lower())


def parse_export_key(path: Path) -> tuple[str, str, str, int] | None:
    stem = path.stem
    batch_m = _BATCH_RE.search(stem)
    if not batch_m:
        return None
    batch = batch_m.group(1)
    id_info = _t_identifier_and_category(stem)
    if not id_info:
        return None
    pid, _ = id_info
    side = _extract_side(stem)
    f_m = _F_RE.search(stem)
    f_num = int(f_m.group(1)) if f_m else 0
    return (batch, pid, side, f_num)


def deduplicate_export_paths(paths: list[Path]) -> tuple[list[Path], int]:
    groups: dict[tuple[str, str, str], tuple[int, Path]] = {}
    unknown: list[Path] = []
    for p in paths:
        key = parse_export_key(p)
        if key is None:
            unknown.append(p)
            continue
        batch, pid, side, f_num = key
        group_key = (batch, pid, side)
        if group_key not in groups or f_num > groups[group_key][0]:
            groups[group_key] = (f_num, p)
    kept = [item[1] for item in groups.values()] + unknown
    return kept, len(paths) - len(kept)


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
        self._build_task_tab(tabs)

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
            "支持格式：A10055、B10055、高10055、低10055、侧10055 或 Y-26100002-027-A；"
            "输入完整 Y 标识时按批次和编号精确匹配，仅输入数字时跨批次搜索。"
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

        filter_hint = QLabel("批次/类别筛选（可选）：选择文件夹后自动扫描所有批次和类别，可添加多个条件，搜索结果将同时按编号和筛选条件过滤")
        filter_hint.setStyleSheet("color: #909399; font-size: 11px;")
        filter_hint.setWordWrap(True)
        tab_layout.addWidget(filter_hint)

        batch_filter_row = QHBoxLayout()
        batch_filter_row.setSpacing(6)
        batch_filter_row.addWidget(QLabel("批次:"))
        self.batch_filter_combo = QComboBox()
        self.batch_filter_combo.setMinimumWidth(140)
        self.batch_filter_combo.setEnabled(False)
        batch_filter_row.addWidget(self.batch_filter_combo)
        self.add_batch_filter_btn = QPushButton("+ 添加批次")
        self.add_batch_filter_btn.setStyleSheet(
            "QPushButton { background-color: #409eff; color: white; padding: 3px 10px; "
            "border-radius: 3px; font-size: 11px; }"
            "QPushButton:hover { background-color: #66b1ff; }"
            "QPushButton:disabled { background-color: #ccc; color: #fff; }"
        )
        self.add_batch_filter_btn.setEnabled(False)
        self.add_batch_filter_btn.clicked.connect(self._add_batch_filter)
        batch_filter_row.addWidget(self.add_batch_filter_btn)

        self.selected_batches_label = QLabel("已选批次：无")
        self.selected_batches_label.setStyleSheet(
            "color: #606266; font-size: 11px; padding: 3px 6px; "
            "background: #f5f7fa; border-radius: 3px;"
        )
        self.selected_batches_label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.selected_batches_label.setWordWrap(True)
        self.selected_batches_label.linkActivated.connect(self._remove_filter_item)
        batch_filter_row.addWidget(self.selected_batches_label, 1)
        tab_layout.addLayout(batch_filter_row)

        cat_filter_row = QHBoxLayout()
        cat_filter_row.setSpacing(6)
        cat_filter_row.addWidget(QLabel("类别:"))
        self.category_filter_combo = QComboBox()
        self.category_filter_combo.setMinimumWidth(140)
        self.category_filter_combo.setEnabled(False)
        cat_filter_row.addWidget(self.category_filter_combo)
        self.add_category_filter_btn = QPushButton("+ 添加类别")
        self.add_category_filter_btn.setStyleSheet(
            "QPushButton { background-color: #67c23a; color: white; padding: 3px 10px; "
            "border-radius: 3px; font-size: 11px; }"
            "QPushButton:hover { background-color: #85ce61; }"
            "QPushButton:disabled { background-color: #ccc; color: #fff; }"
        )
        self.add_category_filter_btn.setEnabled(False)
        self.add_category_filter_btn.clicked.connect(self._add_category_filter)
        cat_filter_row.addWidget(self.add_category_filter_btn)

        self.selected_categories_label = QLabel("已选类别：无")
        self.selected_categories_label.setStyleSheet(
            "color: #606266; font-size: 11px; padding: 3px 6px; "
            "background: #f5f7fa; border-radius: 3px;"
        )
        self.selected_categories_label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.selected_categories_label.setWordWrap(True)
        self.selected_categories_label.linkActivated.connect(self._remove_filter_item)
        cat_filter_row.addWidget(self.selected_categories_label, 1)
        tab_layout.addLayout(cat_filter_row)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.image_search_number = QTextEdit()
        self.image_search_number.setPlaceholderText(
            "输入编号或完整标识符，支持批量（空格/换行/逗号分隔）\n"
            "例如：121 122 123\n"
            "或：Y-26100002-121  Y-26100002-122\n"
            "完整 Y 标识按批次和编号匹配；仅输入数字时跨批次搜索"
        )
        self.image_search_number.setFixedHeight(80)
        self.image_search_number.setStyleSheet(
            "QTextEdit { font-size: 13px; border: 1px solid #ddd; border-radius: 4px; padding: 6px; }"
        )
        search_row.addWidget(self.image_search_number, 1)

        btn_col = QVBoxLayout()
        btn_col.setSpacing(4)
        search_button = QPushButton("批量查询图片")
        search_button.setStyleSheet(
            "QPushButton { background-color: #2f4b7c; color: white; padding: 6px 16px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #3d5f99; }"
        )
        search_button.clicked.connect(self._search_images)
        btn_col.addWidget(search_button)

        select_export_btn = QPushButton("选择导出文件夹")
        select_export_btn.setStyleSheet(
            "QPushButton { background-color: #388e3c; color: white; padding: 6px 16px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #4caf50; }"
        )
        select_export_btn.clicked.connect(self._select_export_folder)
        btn_col.addWidget(select_export_btn)

        self.export_path_label = QLabel("尚未选择导出文件夹")
        self.export_path_label.setStyleSheet(
            "color: #606266; font-size: 11px; padding: 2px 4px; "
            "background: #f5f5f5; border-radius: 3px;"
        )
        self.export_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.export_path_label.setCursor(Qt.IBeamCursor)
        self.export_path_label.setWordWrap(True)
        self.export_path_label.setMinimumWidth(260)
        btn_col.addWidget(self.export_path_label)

        self.export_button = QPushButton("导出图片")
        self.export_button.setEnabled(False)
        self.export_button.setStyleSheet(
            "QPushButton { background-color: #1976d2; color: white; padding: 6px 16px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #1e88e5; }"
            "QPushButton:disabled { background-color: #ccc; color: #fff; }"
        )
        self.export_button.clicked.connect(self._export_search_images)
        btn_col.addWidget(self.export_button)

        btn_col_widget = QWidget()
        btn_col_widget.setLayout(btn_col)
        search_row.addWidget(btn_col_widget)
        tab_layout.addLayout(search_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self._clear_image_search)
        action_row.addWidget(clear_button)

        self.copy_images_button = QPushButton("复制全部图片到剪贴板")
        self.copy_images_button.setEnabled(False)
        self.copy_images_button.clicked.connect(self._copy_all_search_images)
        action_row.addWidget(self.copy_images_button)
        action_row.addStretch()
        tab_layout.addLayout(action_row)

        self.image_search_result_count = QLabel("")
        self.image_search_result_count.setStyleSheet("color: #606266; font-size: 12px;")
        self.image_search_result_count.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.image_search_result_count.setCursor(Qt.IBeamCursor)
        tab_layout.addWidget(self.image_search_result_count)

        self.image_search_ng_list = QListWidget()
        self.image_search_ng_list.setFixedHeight(120)
        self.image_search_ng_list.setStyleSheet(
            "QListWidget { border: 1px solid #f5c6cb; border-radius: 4px; background: #fff5f5; "
            "font-size: 11px; color: #b71c1c; }"
            "QListWidget::item { padding: 2px 6px; }"
        )
        self.image_search_ng_list.hide()
        tab_layout.addWidget(self.image_search_ng_list)

        self.copy_not_found_button = QPushButton("复制未找到编号")
        self.copy_not_found_button.setEnabled(False)
        self.copy_not_found_button.setStyleSheet(
            "QPushButton { background-color: #e53935; color: white; padding: 4px 12px; "
            "border-radius: 4px; font-size: 12px; }"
            "QPushButton:hover { background-color: #c62828; }"
            "QPushButton:disabled { background-color: #ccc; color: #fff; }"
        )
        self.copy_not_found_button.clicked.connect(self._copy_not_found_identifiers)
        action_row.addWidget(self.copy_not_found_button)

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

        previous_export = settings.value(IMAGE_EXPORT_FOLDER_SETTINGS_KEY, "", type=str)
        if previous_export and os.path.isdir(previous_export):
            self._export_parent_dir = previous_export
            self.export_path_label.setText(f"📁 {previous_export}")
            self.export_path_label.setStyleSheet(
                "color: #388e3c; font-size: 11px; padding: 2px 4px; "
                "background: #e8f5e9; border-radius: 3px;"
            )
        else:
            self._export_parent_dir = ""

        self._filter_tree: dict[str, dict[str, list[str]]] = {}
        self._selected_batches: set[str] = set()
        self._selected_categories: set[str] = set()
        if self.image_search_folder:
            self._rebuild_filter_controls()

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
        self.copy_not_found_button.setEnabled(False)
        self.image_search_result_count.clear()
        self.image_search_ng_list.clear()
        self.image_search_ng_list.hide()
        self.image_search_preview.setText("输入编号后查询图片")
        self._current_search_matches = []
        self._not_found_identifiers = []
        self._selected_batches.clear()
        self._selected_categories.clear()
        self._rebuild_filter_controls()

    def _parse_batch_identifiers(self, text: str) -> list[str]:
        raw = re.split(r"[\s,，;；]+", text.strip())
        seen = set()
        result = []
        for item in raw:
            item = item.strip()
            if not item:
                continue
            y_query = _parse_y_search_query(item)
            parsed = f"Y-{y_query[0]}-{y_query[1]}" if y_query else extract_query_identifier(item)
            if not parsed:
                continue
            if parsed in seen:
                continue
            seen.add(parsed)
            result.append(parsed)
        return result

    def _search_images(self):
        folder_path = getattr(self, "image_search_folder", "")
        raw_text = self.image_search_number.toPlainText()
        if not folder_path:
            QMessageBox.warning(self, "提示", "请先选择图片文件夹")
            return
        identifiers = self._parse_batch_identifiers(raw_text)
        if not identifiers:
            QMessageBox.warning(
                self, "提示",
                "请输入有效的数字编号（支持空格/换行/逗号分隔批量输入）"
            )
            return

        all_matches: list[Path] = []
        seen_paths: set[str] = set()
        path_to_identifier: dict[str, str] = {}
        ng_identifiers: set[str] = set()
        ng_file_paths: list[Path] = []
        errors = []
        found_identifiers: set[str] = set()
        for identifier in identifiers:
            try:
                matches = find_images_by_identifier(folder_path, identifier)
            except OSError as exc:
                errors.append(f"编号 {identifier} 扫描失败: {exc}")
                continue
            if matches:
                found_identifiers.add(identifier)
                id_all_ng = all(self._is_ng_path(p, folder_path) for p in matches)
                if id_all_ng:
                    ng_identifiers.add(identifier)
            for p in matches:
                key = str(p).lower()
                if key not in seen_paths:
                    seen_paths.add(key)
                    all_matches.append(p)
                    path_to_identifier[key] = identifier
                    if self._is_ng_path(p, folder_path):
                        ng_file_paths.append(p)

        batch_filter = self._selected_batches
        category_filter = self._selected_categories
        if batch_filter or category_filter:
            filtered_matches: list[Path] = []
            filtered_path_to_identifier: dict[str, str] = {}
            filtered_ng_paths: list[Path] = []
            for p in all_matches:
                b, c = extract_batch_category_from_path(p)
                if batch_filter and b not in batch_filter:
                    continue
                if category_filter and c not in category_filter:
                    continue
                filtered_matches.append(p)
                key = str(p).lower()
                filtered_path_to_identifier[key] = path_to_identifier.get(key, "")
                if self._is_ng_path(p, folder_path):
                    filtered_ng_paths.append(p)
            all_matches = filtered_matches
            path_to_identifier = filtered_path_to_identifier
            ng_file_paths = filtered_ng_paths

        not_found = [i for i in identifiers if i not in found_identifiers]

        self.image_search_results.clear()
        self.image_search_preview.setText("请从左侧选择图片预览")
        if len(identifiers) == 1:
            count_text = f"编号 {identifiers[0]}：找到 {len(all_matches)} 张匹配图片"
        else:
            count_text = f"共 {len(identifiers)} 个编号，找到 {len(all_matches)} 张匹配图片（已去重）"
        filter_parts = []
        if self._selected_batches:
            filter_parts.append(f"批次=[{','.join(sorted(self._selected_batches))}]")
        if self._selected_categories:
            filter_parts.append(f"类别=[{','.join(sorted(self._selected_categories))}]")
        if filter_parts:
            count_text += "　筛选：" + " + ".join(filter_parts)
        if not_found:
            count_text += f"　未查到 {len(not_found)} 个：{' '.join(not_found)}"
        if errors:
            count_text += f"　⚠ {len(errors)} 个失败"
        self.image_search_result_count.setText(count_text)

        self.image_search_ng_list.clear()
        if ng_file_paths:
            ng_names = sorted({p.name for p in ng_file_paths})
            header = QListWidgetItem(
                f"⚠ NG 图片 {len(ng_file_paths)} 张 / 编号 {' '.join(sorted(ng_identifiers))}"
            )
            header.setFlags(Qt.NoItemFlags)
            header.setForeground(QColor("#c62828"))
            header.setBackground(QColor("#ffebee"))
            self.image_search_ng_list.addItem(header)
            for name in ng_names:
                self.image_search_ng_list.addItem(QListWidgetItem(name))
            self.image_search_ng_list.show()
        else:
            self.image_search_ng_list.hide()

        self.image_search_result_count.setTextFormat(Qt.RichText)
        self.copy_images_button.setEnabled(bool(all_matches))
        self.export_button.setEnabled(bool(all_matches) and bool(getattr(self, "_export_parent_dir", "")))
        self._not_found_identifiers = not_found
        self.copy_not_found_button.setEnabled(bool(not_found))

        self._current_search_matches = all_matches

        for path in all_matches:
            path_str = str(path)
            key = path_str.lower()
            identifier = path_to_identifier.get(key, "")
            highlight_id = extract_query_identifier(identifier) if identifier else None
            highlighted = highlight_identifier_in_filename(path_str, highlight_id) if highlight_id else html.escape(path_str)
            prefix = f'<span style="display:inline-block;background:#1a56db;color:white;border-radius:3px;padding:1px 6px;margin-right:6px;font-size:11px;">#{identifier}</span>' if identifier else ""
            item = QListWidgetItem()
            item.setData(Qt.UserRole, path_str)
            item.setToolTip(path_str)
            self.image_search_results.addItem(item)
            label = QLabel(f'<div>{prefix}{highlighted}</div>')
            label.setTextFormat(Qt.RichText)
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            label.setMinimumWidth(460)
            label.setStyleSheet(
                "QLabel { padding: 6px; background: transparent; font-size: 12px; }"
            )
            item.setSizeHint(label.sizeHint())
            self.image_search_results.setItemWidget(item, label)

    def _select_export_folder(self):
        settings = QSettings("ProductionManagement", "Client")
        initial_dir = settings.value(IMAGE_EXPORT_FOLDER_SETTINGS_KEY, "", type=str) or ""
        parent_dir = QFileDialog.getExistingDirectory(
            self, "选择导出根目录（将自动创建 产品名/日期时分 子文件夹）", initial_dir
        )
        if not parent_dir:
            return
        settings.setValue(IMAGE_EXPORT_FOLDER_SETTINGS_KEY, parent_dir)
        self._export_parent_dir = parent_dir
        self.export_path_label.setText(f"📁 {parent_dir}")
        self.export_path_label.setStyleSheet(
            "color: #388e3c; font-size: 11px; padding: 2px 4px; "
            "background: #e8f5e9; border-radius: 3px;"
        )
        has_matches = bool(getattr(self, "_current_search_matches", []))
        self.export_button.setEnabled(has_matches)

    def _export_search_images(self):
        matches = getattr(self, "_current_search_matches", [])
        if not matches:
            QMessageBox.warning(self, "提示", "请先执行图片查询")
            return

        parent_dir = getattr(self, "_export_parent_dir", "")
        if not parent_dir or not os.path.isdir(parent_dir):
            QMessageBox.warning(self, "提示", "请先选择导出文件夹")
            return

        now_str = dt.datetime.now().strftime("%Y%m%d_%H%M")
        search_root = Path(getattr(self, "image_search_folder", ""))
        product_name = self._extract_product_name(str(search_root)) if search_root else "未命名"
        target_dir = Path(parent_dir) / product_name / now_str
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "创建失败", f"无法创建目录：{target_dir}\n{exc}")
            return

        export_matches, dup_count = deduplicate_export_paths(matches)

        copied = 0
        skipped = 0
        errors = []
        root = search_root
        for src in export_matches:
            try:
                rel = src.relative_to(root) if root and src.is_relative_to(root) else Path(src.name)
            except ValueError:
                rel = Path(src.name)
            dst = target_dir / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
            except OSError as exc:
                if os.path.exists(dst):
                    skipped += 1
                else:
                    errors.append(f"{rel}: {exc}")

        msg_lines = [
            f"导出完成！",
            f"产品：{product_name}",
            f"时间：{now_str}",
            f"完整路径：{target_dir}",
            f"",
            f"成功复制：{copied} 个",
            f"跳过已存在：{skipped} 个",
        ]
        if dup_count:
            msg_lines.append(f"复拍去重：{dup_count} 个（仅保留最后复拍）")
        msg_lines.append(f"原始匹配：{len(matches)} 个 → 实际导出：{len(export_matches)} 个")
        if errors:
            msg_lines.append(f"失败：{len(errors)} 个")
            msg_lines.append("\n".join(errors[:5]))
            if len(errors) > 5:
                msg_lines.append(f"... 等 {len(errors)} 个错误")

        self.export_path_label.setText(f"✅ {target_dir}　(成功 {copied} / 跳过 {skipped})")
        QMessageBox.information(self, "导出完成", "\n".join(msg_lines))

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

    def _copy_not_found_identifiers(self):
        not_found = getattr(self, "_not_found_identifiers", [])
        if not not_found:
            QMessageBox.information(self, "提示", "当前没有未找到的编号")
            return
        text = " ".join(not_found)
        QGuiApplication.clipboard().setText(text)
        self.copy_not_found_button.setText("已复制 ✓")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1500, lambda: self.copy_not_found_button.setText("复制未找到编号"))

    def _clear_image_search(self):
        self.image_search_folder = ""
        self.image_search_folder_label.setText("尚未选择文件夹")
        self.image_search_number.clear()
        self.image_search_results.clear()
        self.copy_images_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.copy_not_found_button.setEnabled(False)
        self.image_search_result_count.clear()
        self.image_search_ng_list.clear()
        self.image_search_ng_list.hide()
        self.image_search_preview.setText("查询结果图片会在这里预览")
        self._current_search_matches = []
        self._not_found_identifiers = []
        self._selected_batches.clear()
        self._selected_categories.clear()
        self.batch_filter_combo.blockSignals(True)
        self.batch_filter_combo.clear()
        self.batch_filter_combo.blockSignals(False)
        self.category_filter_combo.blockSignals(True)
        self.category_filter_combo.clear()
        self.category_filter_combo.blockSignals(False)
        self.batch_filter_combo.setEnabled(False)
        self.category_filter_combo.setEnabled(False)
        self.add_batch_filter_btn.setEnabled(False)
        self.add_category_filter_btn.setEnabled(False)
        self.selected_batches_label.setText("已选批次：无")
        self.selected_categories_label.setText("已选类别：无")
        self.export_path_label.setText("尚未选择导出文件夹")
        self.export_path_label.setStyleSheet(
            "color: #606266; font-size: 11px; padding: 2px 4px; "
            "background: #f5f5f5; border-radius: 3px;"
        )

    def _rebuild_filter_controls(self):
        folder_path = getattr(self, "image_search_folder", "")
        if not folder_path:
            self._filter_tree = {}
            self.batch_filter_combo.clear()
            self.batch_filter_combo.setEnabled(False)
            self.add_batch_filter_btn.setEnabled(False)
            self.category_filter_combo.clear()
            self.category_filter_combo.setEnabled(False)
            self.add_category_filter_btn.setEnabled(False)
            return

        self._filter_tree = scan_batch_category_tree(folder_path)
        batches = sorted(self._filter_tree.keys())

        self.batch_filter_combo.blockSignals(True)
        self.batch_filter_combo.clear()
        self.batch_filter_combo.addItem("-- 选择批次 --")
        self.batch_filter_combo.addItems(batches)
        self.batch_filter_combo.setCurrentIndex(0)
        self.batch_filter_combo.blockSignals(False)
        has_batches = len(batches) > 0
        self.batch_filter_combo.setEnabled(has_batches)
        self.add_batch_filter_btn.setEnabled(has_batches)

        categories: set[str] = set()
        for cats in self._filter_tree.values():
            categories.update(cats.keys())
        cat_list = sorted(categories)

        self.category_filter_combo.blockSignals(True)
        self.category_filter_combo.clear()
        self.category_filter_combo.addItem("-- 选择类别 --")
        self.category_filter_combo.addItems(cat_list)
        self.category_filter_combo.setCurrentIndex(0)
        self.category_filter_combo.blockSignals(False)
        has_cats = len(cat_list) > 0
        self.category_filter_combo.setEnabled(has_cats)
        self.add_category_filter_btn.setEnabled(has_cats)

        self._update_selected_filters_display()

    def _add_batch_filter(self):
        batch = self.batch_filter_combo.currentText()
        if not batch or batch.startswith("--"):
            return
        self._selected_batches.add(batch)
        self.batch_filter_combo.setCurrentIndex(0)
        self._update_selected_filters_display()

    def _add_category_filter(self):
        category = self.category_filter_combo.currentText()
        if not category or category.startswith("--"):
            return
        self._selected_categories.add(category)
        self.category_filter_combo.setCurrentIndex(0)
        self._update_selected_filters_display()

    def _update_selected_filters_display(self):
        if self._selected_batches:
            items = sorted(self._selected_batches)
            html_parts = []
            for b in items:
                html_parts.append(
                    f'<span style="display:inline-block;background:#ecf5ff;color:#409eff;'
                    f'border:1px solid #b3d8ff;border-radius:3px;padding:1px 6px;margin:1px 3px 1px 0px;'
                    f'font-size:11px;">{b} <a style="color:#f56c6c;text-decoration:none;'
                    f'cursor:pointer;" href="batch:{b}">✕</a></span>'
                )
            self.selected_batches_label.setText(
                "已选批次：" + "".join(html_parts) + f' <a href="batch:__clear__" '
                f'style="color:#909399;font-size:10px;text-decoration:none;">全部清除</a>'
            )
        else:
            self.selected_batches_label.setText("已选批次：无")

        if self._selected_categories:
            items = sorted(self._selected_categories)
            html_parts = []
            for c in items:
                html_parts.append(
                    f'<span style="display:inline-block;background:#f0f9eb;color:#67c23a;'
                    f'border:1px solid #c2e7b0;border-radius:3px;padding:1px 6px;margin:1px 3px 1px 0px;'
                    f'font-size:11px;">{c} <a style="color:#f56c6c;text-decoration:none;'
                    f'cursor:pointer;" href="category:{c}">✕</a></span>'
                )
            self.selected_categories_label.setText(
                "已选类别：" + "".join(html_parts) + f' <a href="category:__clear__" '
                f'style="color:#909399;font-size:10px;text-decoration:none;">全部清除</a>'
            )
        else:
            self.selected_categories_label.setText("已选类别：无")

    def _remove_filter_item(self, link: str):
        if link.startswith("batch:"):
            item = link[6:]
            if item == "__clear__":
                self._selected_batches.clear()
            else:
                self._selected_batches.discard(item)
        elif link.startswith("category:"):
            item = link[9:]
            if item == "__clear__":
                self._selected_categories.clear()
            else:
                self._selected_categories.discard(item)
        self._update_selected_filters_display()

    # ------------------------------------------------------------------
    # 良品 / NG 统计
    # ------------------------------------------------------------------
    def _build_ng_stats_tab(self, tabs):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(16, 16, 16, 16)
        tab_layout.setSpacing(10)

        hint_row = QHBoxLayout()
        hint = QLabel(
            "选择需统计文件夹：根目录直接存放良品图，子文件夹NG存放NG图。\n"
            "支持同时选择多个文件夹合并统计；同一批次编号跨文件夹会自动合并。\n"
            "支持文件名格式：高1、低1、侧1、A1、B1、Y-26100002-027-A（编号027，A面）、\n"
            "以及 T-20261113-高1-L背面精加-R1（类别为 L背面精加，R1 复拍标记可选）和旧版 T 开头格式。"
        )
        hint.setStyleSheet("color: #666; font-size: 12px;")
        hint.setWordWrap(True)
        hint_row.addWidget(hint, 1)

        self._product_max_label = QLabel("")
        self._product_max_label.setStyleSheet(
            "QLabel { color: #2f4b7c; font-size: 13px; font-weight: bold; "
            "background: #f0f4ff; border: 1px solid #c7d7f5; border-radius: 4px; "
            "padding: 8px 12px; }"
        )
        self._product_max_label.setWordWrap(True)
        self._product_max_label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        hint_row.addWidget(self._product_max_label)
        tab_layout.addLayout(hint_row)

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
        batch_row.addWidget(QLabel("类别："))
        self.stats_category_filter = QComboBox()
        self.stats_category_filter.addItem("全部类别", "")
        self.stats_category_filter.setMinimumWidth(140)
        self.stats_category_filter.setStyleSheet(
            "QComboBox { padding: 4px; border: 1px solid #ddd; border-radius: 4px; }"
        )
        self.stats_category_filter.currentIndexChanged.connect(
            self._refresh_ng_stats_product_filter
        )
        batch_row.addWidget(self.stats_category_filter)
        batch_row.addWidget(QLabel("产品编号："))
        self.stats_product_filter = QComboBox()
        self.stats_product_filter.addItem("全部编号", "")
        self.stats_product_filter.setMinimumWidth(100)
        self.stats_product_filter.setStyleSheet(
            "QComboBox { padding: 4px; border: 1px solid #ddd; border-radius: 4px; }"
        )
        batch_row.addWidget(self.stats_product_filter)
        batch_row.addWidget(QLabel("（点击统计应用筛选）"))
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
        self.ng_folder_list.itemDoubleClicked.connect(self._open_ng_folder)
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

        self._stats_category_products: dict[tuple[str, str, str], set[str]] = {}
        self.batch_filter.currentIndexChanged.connect(
            self._refresh_ng_stats_category_filter
        )
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
        self._refresh_product_max_ids()

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
        self._refresh_product_max_ids()

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
        self._refresh_product_max_ids()

    def _clear_ng_folders(self):
        self.ng_folder_list.clear()
        self.stats_btn.setEnabled(False)
        self._reset_ng_batch_filter()
        settings = QSettings("ProductionManagement", "Client")
        settings.setValue(NG_FOLDER_SETTINGS_KEY, "")
        self._refresh_product_max_ids()

    _BATCH_DIR_RE = re.compile(r"^\d{8}", re.IGNORECASE)
    _BATCH_DATE_RE = re.compile(r"(\d{8})")

    def _extract_product_name(self, folder_path: str) -> str:
        parts = Path(folder_path).parts
        for part in reversed(parts):
            if not part:
                continue
            if "Y-" in part.upper():
                continue
            if self._BATCH_DIR_RE.match(part):
                continue
            return part
        return os.path.basename(folder_path) or folder_path

    def _extract_batch_date(self, batch_dir_name: str) -> str:
        m = self._BATCH_DATE_RE.search(batch_dir_name)
        return m.group(1) if m else ""

    def _pid_from_filename(self, name: str) -> int | None:
        name_without_ext = os.path.splitext(name)[0]
        y_match = Y_PATTERN.search(name)
        if y_match:
            _, pid, _ = _parse_y_match(y_match)
            return int(pid)
        t_match = T_PATTERN.match(name_without_ext)
        if t_match:
            identifier_and_category = _t_identifier_and_category(name_without_ext)
            return int(identifier_and_category[0]) if identifier_and_category else None
        side_match = _SIDE_NUMBER_RE.search(name)
        if side_match:
            return int(side_match.group(2))
        for m in _STANDALONE_NUMBER_RE.finditer(name):
            if not _DATE8_RE.fullmatch(m.group(1)):
                return int(m.group(1))
        return None

    @staticmethod
    def _is_ng_path(file_path: Path | str, root_path: Path | str | None = None) -> bool:
        path_str = str(file_path)
        name = os.path.basename(path_str)
        name_without_ext = os.path.splitext(name)[0]
        filename_says_ng = "NG" in name_without_ext.upper()
        if filename_says_ng:
            return True
        if root_path is not None:
            try:
                rel_root = os.path.relpath(os.path.dirname(path_str), str(root_path))
            except ValueError:
                rel_root = path_str
            is_ng_folder = (rel_root != ".") and ("NG" in rel_root.upper())
            return is_ng_folder
        parent = os.path.dirname(path_str)
        return "NG" in parent.upper()

    def _scan_latest_batch_max_pid(self, folder_path: str) -> tuple[str | None, int | None]:
        batch_files: dict[str, list[str]] = {}
        try:
            for root, dirs, filenames in os.walk(folder_path):
                rel = os.path.relpath(root, folder_path)
                if rel == ".":
                    batch_key = "."
                else:
                    first_level = rel.split(os.sep)[0]
                    if self._BATCH_DIR_RE.match(first_level):
                        batch_key = first_level
                    else:
                        batch_key = "."
                batch_files.setdefault(batch_key, []).extend(filenames)
        except OSError:
            return None, None

        if not batch_files:
            return None, None

        sorted_batches = sorted(batch_files.keys(), key=self._extract_batch_date)
        latest_batch = sorted_batches[-1]
        latest_date = self._extract_batch_date(latest_batch)

        max_pid = None
        for names in batch_files.values():
            for name in names:
                pid = self._pid_from_filename(name)
                if pid is not None and (max_pid is None or pid > max_pid):
                    max_pid = pid

        return latest_date or None, max_pid

    def _refresh_product_max_ids(self):
        product_best: dict[str, tuple[str | None, int]] = {}
        for i in range(self.ng_folder_list.count()):
            folder_path = self.ng_folder_list.item(i).text()
            product = self._extract_product_name(folder_path)
            latest_date, max_pid = self._scan_latest_batch_max_pid(folder_path)
            if max_pid is None:
                continue
            prev = product_best.get(product)
            if prev is None:
                product_best[product] = (latest_date, max_pid)
            else:
                prev_date, prev_pid = prev
                best_date = (latest_date or "") >= (prev_date or "") and latest_date or prev_date
                best_pid = max(prev_pid, max_pid)
                product_best[product] = (best_date, best_pid)

        if not product_best:
            self._product_max_label.setText("")
            self._product_max_label.hide()
            return

        lines = ["各产品当前最大编号（最新批次）："]
        for product, (date_str, max_pid) in sorted(product_best.items()):
            date_tag = f" [{date_str}]" if date_str else ""
            lines.append(f"  {product}{date_tag}　→　{max_pid}")
        self._product_max_label.setText("\n".join(lines))
        self._product_max_label.show()

    def _open_ng_folder(self, item):
        folder_path = item.text()
        if folder_path and os.path.isdir(folder_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder_path))
        else:
            QMessageBox.warning(self, "提示", f"文件夹不存在：\n{folder_path}")

    def _analyze_ng_stats(self):
        all_parent_paths = [self.ng_folder_list.item(i).text() for i in range(self.ng_folder_list.count())]
        if not all_parent_paths:
            QMessageBox.warning(self, "提示", "请先添加需统计的文件夹")
            return

        range_start_text = self.range_start.text().strip()
        range_end_text = self.range_end.text().strip()
        range_start = int(range_start_text) if range_start_text else None
        range_end = int(range_end_text) if range_end_text else None

        all_ok_ids_by_batch: dict[str, set[str]] = {}
        all_ng_ids_by_batch: dict[str, set[str]] = {}
        pid_ctimes: dict[str, list[float]] = {}
        batch_ctimes: dict[str, list[float]] = {}
        ok_filenames = []
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
                        t_match = T_PATTERN.match(name_without_ext)
                        if t_match:
                            batch = t_match.group(1)
                        else:
                            y_match = Y_PATTERN.search(name)
                            if y_match:
                                batch = y_match.group(1)
                            else:
                                batch = "老格式"
                        pid = self._pid_from_filename(name)
                        if pid is None:
                            if is_ng:
                                invalid_entries.append((parent_path, full_path))
                            continue
                        pid_str = str(pid)
                        ckey = f"{batch}|{pid_str}"
                        ftime = None
                        try:
                            ftime = os.path.getmtime(full_path)
                            batch_ctimes.setdefault(batch, []).append(ftime)
                            pid_ctimes.setdefault(ckey, []).append(ftime)
                        except OSError:
                            pass
                        if is_ng:
                            all_ng_ids_by_batch.setdefault(batch, set()).add(pid_str)
                            ng_defect_filenames.append(name)
                        else:
                            all_ok_ids_by_batch.setdefault(batch, set()).add(pid_str)
                            ok_filenames.append(name)
        except OSError as exc:
            QMessageBox.critical(self, "读取失败", f"读取文件夹失败：{exc}")
            return

        use_time_filter = self._ng_dt_start is not None or self._ng_dt_end is not None

        def _pid_in_time_range(ckey: str) -> bool:
            times = pid_ctimes.get(ckey)
            if not times:
                return not use_time_filter
            for ts in times:
                dt = QDateTime.fromSecsSinceEpoch(int(ts))
                if self._ng_dt_start is not None and dt < self._ng_dt_start:
                    continue
                if self._ng_dt_end is not None and dt >= self._ng_dt_end:
                    continue
                return True
            return False

        ok_ids_by_batch: dict[str, set[str]] = {}
        ng_ids_by_batch: dict[str, set[str]] = {}
        for batch in set(all_ok_ids_by_batch.keys()) | set(all_ng_ids_by_batch.keys()):
            for pid_str in all_ok_ids_by_batch.get(batch, set()):
                ckey = f"{batch}|{pid_str}"
                if range_start is not None and int(pid_str) < range_start:
                    continue
                if range_end is not None and int(pid_str) > range_end:
                    continue
                if use_time_filter and not _pid_in_time_range(ckey):
                    continue
                ok_ids_by_batch.setdefault(batch, set()).add(pid_str)
            for pid_str in all_ng_ids_by_batch.get(batch, set()):
                ckey = f"{batch}|{pid_str}"
                if range_start is not None and int(pid_str) < range_start:
                    continue
                if range_end is not None and int(pid_str) > range_end:
                    continue
                if use_time_filter and not _pid_in_time_range(ckey):
                    continue
                ng_ids_by_batch.setdefault(batch, set()).add(pid_str)

        ok_category_ids = extract_ng_defect_category_ids(ok_filenames)
        ng_category_ids = extract_ng_defect_category_ids(ng_defect_filenames)
        self._stats_category_products = {}
        for category_ids, status_ids_by_batch in (
            (ok_category_ids, ok_ids_by_batch),
            (ng_category_ids, ng_ids_by_batch),
        ):
            for batch, cats in category_ids.items():
                valid_ids = status_ids_by_batch.get(batch, set())
                for category, sides in cats.items():
                    for side, pids in sides.items():
                        matching = pids & valid_ids
                        if matching:
                            self._stats_category_products.setdefault(
                                (batch, category, side), set()
                            ).update(matching)

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
        self._refresh_ng_stats_category_filter()
        selected_category = self.stats_category_filter.currentData() or ""
        selected_product = self.stats_product_filter.currentData() or ""

        batches = (
            [selected_batch]
            if selected_batch
            else available_batches
        )

        def category_filtered_ids(
            category_ids: dict[str, dict[str, dict[str, set[str]]]],
            batch: str,
        ) -> set[str]:
            matching_ids: set[str] = set()
            for cat_batch, cats in category_ids.items():
                if cat_batch != batch:
                    continue
                for category, sides in cats.items():
                    if selected_category and category != selected_category:
                        continue
                    for side, pids in sides.items():
                        matching_ids.update(pids)
            if selected_product:
                matching_ids.intersection_update({selected_product})
            return matching_ids

        batch_stats = {}
        final_ok_all = set()
        final_ng_all = set()
        for batch in batches:
            ok_set = ok_ids_by_batch.get(batch, set())
            ng_set = ng_ids_by_batch.get(batch, set())
            if selected_category or selected_product:
                ok_set = ok_set & category_filtered_ids(ok_category_ids, batch)
                ng_set = ng_set & category_filtered_ids(ng_category_ids, batch)
            final_ok = sorted(ok_set - ng_set, key=int)
            final_ng = sorted(ng_set, key=int)
            batch_stats[batch] = (final_ok, final_ng)
            final_ok_all.update(final_ok)
            final_ng_all.update(final_ng)

        total = len(final_ok_all) + len(final_ng_all)

        def filter_category_products(
            category_ids: dict[str, dict[str, dict[str, set[str]]]],
            valid_ids_by_batch: dict[str, set[str]],
        ) -> dict[tuple[str, str, str], set[str]]:
            filtered: dict[tuple[str, str, str], set[str]] = {}
            for batch, cats in category_ids.items():
                if selected_batch and batch != selected_batch:
                    continue
                batch_valid = valid_ids_by_batch.get(batch, set())
                for category, sides in cats.items():
                    if selected_category and category != selected_category:
                        continue
                    for side, pids in sides.items():
                        matching = pids & batch_valid
                        if selected_product:
                            matching &= {selected_product}
                        if matching:
                            filtered[(batch, category, side)] = matching
            return filtered

        good_ids_by_batch = {
            batch: ids - ng_ids_by_batch.get(batch, set())
            for batch, ids in ok_ids_by_batch.items()
        }
        good_products_by_category = filter_category_products(
            ok_category_ids,
            good_ids_by_batch,
        )
        ng_products_by_category = filter_category_products(
            ng_category_ids,
            ng_ids_by_batch,
        )
        defect_ids_by_category = ng_products_by_category

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
            ng_rate_all = len(final_ng_all) / total * 100 if total else 0
            table_lines.append(
                f"<table style='border-collapse:collapse;width:100%;margin-bottom:8px;background:#fafafa;border:1px solid #e0e0e0;'>"
                f"<tr><td colspan='2' style='height:32px;padding:6px;text-align:center;font-size:14px;font-weight:bold;color:#303133;'>"
                f"汇总　|　总计 {total} 个　|　良品 {len(final_ok_all)}　|　NG {len(final_ng_all)}　|　不良率 {ng_rate_all:.2f}%"
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

            batch_rate = len(final_ng) / batch_total * 100 if batch_total else 0
            table_lines.append("<table style='border-collapse:collapse;width:100%;margin-bottom:8px;'>")
            table_lines.append(
                f"<tr><td colspan='2' style='height:32px;padding:6px;text-align:center;font-size:14px;font-weight:bold;"
                f"background:#f0f4ff;color:#1a56db;border:1px solid #c7d7f5;'>"
                f"批次 {batch}　|　总计 {batch_total} 个　|　良品 {len(final_ok)}　|　NG {len(final_ng)}　|　不良率 {batch_rate:.2f}%"
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
            "<tr><th colspan='4' style='border:1px solid #ccc;padding:8px;text-align:center;"
            "background:#f5f7fa;color:#303133;font-size:15px;'>NG 不良明细</th></tr>"
        )
        table_lines.append(
            "<tr>"
            "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#f5f7fa;'>批次 / 类别</th>"
            "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#f5f7fa;width:50px;'>面</th>"
            "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#f5f7fa;'>数量 / 不良率</th>"
            "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#f5f7fa;'>对应 NG 编号</th>"
            "</tr>"
        )
        side_order = ("高", "低", "侧", "A", "B", "C", "左", "右", "-")

        def _sort_sides(sides: set[str]) -> list[str]:
            return sorted(sides, key=lambda s: (side_order.index(s) if s in side_order else 99, s))

        ng_grouped: dict[tuple[str, str], dict[str, set[str]]] = {}
        for (batch, category, side), pids in defect_ids_by_category.items():
            ng_grouped.setdefault((batch, category), {})[side] = pids

        for batch in batches:
            batch_total = len(batch_stats[batch][0]) + len(batch_stats[batch][1])
            batch_groups = {
                k: v for k, v in ng_grouped.items() if k[0] == batch
            }
            if not batch_groups:
                continue
            for (b, category), side_map in sorted(batch_groups.items()):
                sorted_sides = _sort_sides(set(side_map.keys()))
                rowspan = len(sorted_sides)
                for idx, side in enumerate(sorted_sides):
                    pids = side_map[side]
                    count = len(pids)
                    rate = count / batch_total * 100 if batch_total else 0
                    identifiers = "、".join(sorted(pids, key=int))
                    table_lines.append("<tr>")
                    if idx == 0:
                        table_lines.append(
                            f"<td rowspan='{rowspan}' style='border:1px solid #ccc;padding:6px;"
                            f"text-align:center;font-weight:bold;vertical-align:middle;'>"
                            f"{batch} / {html.escape(category)}</td>"
                        )
                    table_lines.append(
                        f"<td style='border:1px solid #ccc;padding:6px;text-align:center;'>{side}</td>"
                        f"<td style='border:1px solid #ccc;padding:6px;text-align:center;color:#c62828;'>"
                        f"{count} 个 / {rate:.2f}%</td>"
                        f"<td style='border:1px solid #ccc;padding:6px;text-align:left;color:#c62828;'>{identifiers}</td>"
                    )
                    table_lines.append("</tr>")
        if not defect_ids_by_category:
            table_lines.append(
                "<tr><td colspan='4' style='border:1px solid #ccc;padding:8px;text-align:center;color:#909399;'>"
                "未识别到带类别前缀的 NG 编号</td></tr>"
            )
        table_lines.append(
            "</table>"
            "<table style='border-collapse:collapse;width:100%;margin-top:12px;margin-left:-80px;'>"
            "<tr><th colspan='4' style='border:1px solid #ccc;padding:8px;text-align:center;"
            "background:#f5f7fa;color:#303133;font-size:15px;'>批次 / 类别产品明细</th></tr>"
            "<tr>"
            "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#f5f7fa;'>批次 / 类别</th>"
            "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#f5f7fa;width:50px;'>面</th>"
            "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#e8f5e9;color:#2e7d32;'>良品编号</th>"
            "<th style='border:1px solid #ccc;padding:6px;text-align:center;background:#ffebee;color:#c62828;'>NG 编号</th>"
            "</tr>"
        )
        all_product_keys = set(good_products_by_category) | set(ng_products_by_category)
        product_grouped: dict[tuple[str, str], set[str]] = {}
        for (batch, category, side) in all_product_keys:
            product_grouped.setdefault((batch, category), set()).add(side)
        for (batch, category), sides in sorted(product_grouped.items()):
            sorted_sides = _sort_sides(sides)
            rowspan = len(sorted_sides)
            for idx, side in enumerate(sorted_sides):
                good_ids = good_products_by_category.get((batch, category, side), set())
                ng_ids = ng_products_by_category.get((batch, category, side), set())
                good_str = "、".join(sorted(good_ids, key=int))
                ng_str = "、".join(sorted(ng_ids, key=int))
                table_lines.append("<tr>")
                if idx == 0:
                    table_lines.append(
                        f"<td rowspan='{rowspan}' style='border:1px solid #ccc;padding:6px;"
                        f"text-align:center;font-weight:bold;vertical-align:middle;'>"
                        f"{html.escape(batch)} / {html.escape(category)}</td>"
                    )
                table_lines.append(
                    f"<td style='border:1px solid #ccc;padding:6px;text-align:center;'>{side}</td>"
                    f"<td style='border:1px solid #ccc;padding:6px;text-align:center;color:#2e7d32;'>{good_str}</td>"
                    f"<td style='border:1px solid #ccc;padding:6px;text-align:center;color:#c62828;'>{ng_str}</td>"
                )
                table_lines.append("</tr>")
        if not all_product_keys:
            table_lines.append(
                "<tr><td colspan='4' style='border:1px solid #ccc;padding:8px;text-align:center;color:#909399;'>"
                "当前筛选条件下没有已分类产品</td></tr>"
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
        self._stats_category_products = {}
        self.batch_filter.clear()
        self.batch_filter.addItem("全部批次", "")
        self._refresh_ng_stats_category_filter()

    def _refresh_ng_stats_category_filter(self, *_):
        selected_category = self.stats_category_filter.currentData() or ""
        batch = self.batch_filter.currentData() or ""
        categories = sorted({
            category
            for item_batch, category, _side in self._stats_category_products
            if not batch or item_batch == batch
        })

        self.stats_category_filter.blockSignals(True)
        self.stats_category_filter.clear()
        self.stats_category_filter.addItem("全部类别", "")
        for category in categories:
            self.stats_category_filter.addItem(category, category)
        category_index = self.stats_category_filter.findData(selected_category)
        self.stats_category_filter.setCurrentIndex(max(category_index, 0))
        self.stats_category_filter.blockSignals(False)
        self._refresh_ng_stats_product_filter()

    def _refresh_ng_stats_product_filter(self, *_):
        selected_product = self.stats_product_filter.currentData() or ""
        batch = self.batch_filter.currentData() or ""
        category = self.stats_category_filter.currentData() or ""
        products: set[str] = set()
        for (item_batch, item_category, _side), ids in self._stats_category_products.items():
            if batch and item_batch != batch:
                continue
            if category and item_category != category:
                continue
            products.update(ids)

        self.stats_product_filter.blockSignals(True)
        self.stats_product_filter.clear()
        self.stats_product_filter.addItem("全部编号", "")
        for product_id in sorted(products, key=int):
            self.stats_product_filter.addItem(product_id, product_id)
        product_index = self.stats_product_filter.findData(selected_product)
        self.stats_product_filter.setCurrentIndex(max(product_index, 0))
        self.stats_product_filter.blockSignals(False)

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

    # ------------------------------------------------------------------
    # 工程人员每日任务进度
    # ------------------------------------------------------------------
    def _build_task_tab(self, tabs):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(16, 16, 16, 16)
        tab_layout.setSpacing(10)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(10)

        top_bar.addWidget(QLabel("人员："))
        self.task_person_combo = QComboBox()
        self.task_person_combo.setEditable(True)
        self.task_person_combo.setMinimumWidth(140)
        self.task_person_combo.setStyleSheet(
            "QComboBox { padding: 4px 8px; border: 1px solid #dcdfe6; border-radius: 4px; }"
        )
        self.task_person_combo.currentTextChanged.connect(self._on_task_person_changed)
        top_bar.addWidget(self.task_person_combo)

        add_person_btn = QPushButton("＋新建人员")
        add_person_btn.setStyleSheet(
            "QPushButton { background-color: #2f4b7c; color: white; padding: 4px 10px; "
            "border-radius: 4px; font-size: 12px; }"
            "QPushButton:hover { background-color: #3a5a94; }"
        )
        add_person_btn.clicked.connect(self._add_task_person)
        top_bar.addWidget(add_person_btn)

        del_person_btn = QPushButton("删除人员")
        del_person_btn.setStyleSheet(
            "QPushButton { background-color: #c0c4cc; color: white; padding: 4px 10px; "
            "border-radius: 4px; font-size: 12px; }"
            "QPushButton:hover { background-color: #909399; }"
        )
        del_person_btn.clicked.connect(self._delete_task_person)
        top_bar.addWidget(del_person_btn)

        top_bar.addSpacing(20)

        top_bar.addWidget(QLabel("日期："))
        self.task_date_edit = QDateEdit()
        self.task_date_edit.setCalendarPopup(True)
        self.task_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.task_date_edit.setDate(QDate.currentDate())
        self.task_date_edit.setStyleSheet(
            "QDateEdit { padding: 4px 8px; border: 1px solid #dcdfe6; border-radius: 4px; }"
        )
        self.task_date_edit.dateChanged.connect(self._on_task_date_changed)
        top_bar.addWidget(self.task_date_edit)

        today_btn = QPushButton("今天")
        today_btn.setStyleSheet(
            "QPushButton { background-color: #e8f0fe; color: #1a56db; padding: 4px 12px; "
            "border-radius: 4px; border: 1px solid #1a56db; font-size: 12px; }"
            "QPushButton:hover { background-color: #d4e3fc; }"
        )
        today_btn.clicked.connect(lambda: self.task_date_edit.setDate(QDate.currentDate()))
        top_bar.addWidget(today_btn)

        top_bar.addStretch(1)

        self.task_summary_label = QLabel("")
        self.task_summary_label.setStyleSheet(
            "color: #2f4b7c; font-size: 13px; font-weight: bold; padding: 2px 10px; "
            "background: #f0f5ff; border-radius: 4px;"
        )
        top_bar.addWidget(self.task_summary_label)

        tab_layout.addLayout(top_bar)

        form_box = QGroupBox("新增 / 编辑任务")
        form_box.setStyleSheet(
            "QGroupBox { font-weight: bold; color: #2f4b7c; border: 1px solid #e4e7ed; "
            "border-radius: 4px; margin-top: 10px; padding-top: 10px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }"
        )
        form_layout = QHBoxLayout(form_box)
        form_layout.setSpacing(8)

        form_layout.addWidget(QLabel("任务："))
        self.task_content_edit = QLineEdit()
        self.task_content_edit.setPlaceholderText("输入今日任务描述，例如：完成 A 客户方案初稿")
        self.task_content_edit.setStyleSheet(
            "QLineEdit { padding: 5px 10px; border: 1px solid #dcdfe6; border-radius: 4px; }"
        )
        form_layout.addWidget(self.task_content_edit, 3)

        form_layout.addWidget(QLabel("状态："))
        self.task_status_combo = QComboBox()
        for key, label in TASK_STATUS_LABELS.items():
            self.task_status_combo.addItem(label, key)
        self.task_status_combo.setStyleSheet(
            "QComboBox { padding: 5px 8px; border: 1px solid #dcdfe6; border-radius: 4px; min-width: 90px; }"
        )
        form_layout.addWidget(self.task_status_combo)

        form_layout.addWidget(QLabel("进度："))
        self.task_progress_spin = QSpinBox()
        self.task_progress_spin.setRange(0, 100)
        self.task_progress_spin.setSuffix(" %")
        self.task_progress_spin.setStyleSheet(
            "QSpinBox { padding: 5px 8px; border: 1px solid #dcdfe6; border-radius: 4px; min-width: 80px; }"
        )
        form_layout.addWidget(self.task_progress_spin)

        add_task_btn = QPushButton("添加")
        add_task_btn.setStyleSheet(
            "QPushButton { background-color: #67c23a; color: white; padding: 5px 18px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #85ce61; }"
        )
        add_task_btn.clicked.connect(self._add_or_update_task)
        form_layout.addWidget(add_task_btn)

        self.task_cancel_btn = QPushButton("取消编辑")
        self.task_cancel_btn.setEnabled(False)
        self.task_cancel_btn.setStyleSheet(
            "QPushButton { background-color: #909399; color: white; padding: 5px 14px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #a6a9ad; }"
            "QPushButton:disabled { background-color: #dcdfe6; color: #fff; }"
        )
        self.task_cancel_btn.clicked.connect(self._cancel_task_edit)
        form_layout.addWidget(self.task_cancel_btn)

        tab_layout.addWidget(form_box)

        self.task_table = QTableWidget(0, 6)
        self.task_table.setHorizontalHeaderLabels(["", "任务内容", "状态", "进度", "备注", "操作"])
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.task_table.setAlternatingRowColors(True)
        self.task_table.setStyleSheet(
            "QTableWidget { gridline-color: #ebeef5; alternate-background-color: #fafbfc; "
            "border: 1px solid #e4e7ed; border-radius: 4px; }"
            "QHeaderView::section { background-color: #f5f7fa; padding: 6px 8px; "
            "border: none; border-right: 1px solid #ebeef5; font-weight: bold; color: #606266; }"
        )
        header = self.task_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.task_table.doubleClicked.connect(self._on_task_double_clicked)
        tab_layout.addWidget(self.task_table, 1)

        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(8)

        week_btn = QPushButton("查看本周汇总")
        week_btn.setStyleSheet(
            "QPushButton { background-color: #1976d2; color: white; padding: 5px 14px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #1e88e5; }"
        )
        week_btn.clicked.connect(self._show_weekly_summary)
        bottom_bar.addWidget(week_btn)

        export_btn = QPushButton("导出本日任务")
        export_btn.setStyleSheet(
            "QPushButton { background-color: #2f4b7c; color: white; padding: 5px 14px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #3a5a94; }"
        )
        export_btn.clicked.connect(self._export_today_tasks)
        bottom_bar.addWidget(export_btn)

        bottom_bar.addStretch(1)

        self.task_edit_id = None
        self._task_data = _load_tasks_data()
        self._init_task_person_combo()
        tabs.addTab(tab, "我的任务")

    # --- task data helpers ---
    def _init_task_person_combo(self):
        settings = QSettings("ProductionManagement", "Client")
        saved = settings.value(TASK_PERSON_SETTINGS_KEY, "", type=str)
        self.task_person_combo.blockSignals(True)
        self.task_person_combo.clear()
        persons = list(self._task_data.keys())
        if not persons:
            persons = ["默认"]
            self._task_data["默认"] = {}
        self.task_person_combo.addItems(persons)
        if saved and saved in persons:
            self.task_person_combo.setCurrentText(saved)
        else:
            self.task_person_combo.setCurrentText(persons[0])
        self.task_person_combo.blockSignals(False)
        self._refresh_task_table()

    def _current_person(self) -> str:
        return self.task_person_combo.currentText().strip()

    def _current_date_str(self) -> str:
        return self.task_date_edit.date().toString("yyyy-MM-dd")

    def _on_task_person_changed(self, text: str):
        text = text.strip()
        if not text:
            return
        self._task_data.setdefault(text, {})
        _save_tasks_data(self._task_data)
        QSettings("ProductionManagement", "Client").setValue(TASK_PERSON_SETTINGS_KEY, text)
        self._refresh_task_table()

    def _on_task_date_changed(self):
        self.task_edit_id = None
        self.task_cancel_btn.setEnabled(False)
        self._refresh_task_table()

    def _add_task_person(self):
        name, ok = QInputDialog.getText(self, "新建人员", "请输入人员姓名：")
        if ok and name.strip():
            name = name.strip()
            if name not in self._task_data:
                self._task_data[name] = {}
                _save_tasks_data(self._task_data)
                self.task_person_combo.addItem(name)
            self.task_person_combo.setCurrentText(name)

    def _delete_task_person(self):
        name = self._current_person()
        if name in ("默认", ""):
            QMessageBox.information(self, "提示", "默认人员不可删除")
            return
        ret = QMessageBox.question(
            self, "确认", f"确定删除人员「{name}」及其所有任务记录？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret == QMessageBox.Yes:
            self._task_data.pop(name, None)
            _save_tasks_data(self._task_data)
            idx = self.task_person_combo.findText(name)
            if idx >= 0:
                self.task_person_combo.removeItem(idx)
            if self.task_person_combo.count() == 0:
                self._task_data["默认"] = {}
                self.task_person_combo.addItem("默认")
            self.task_person_combo.setCurrentIndex(0)

    # --- add / edit / delete ---
    def _add_or_update_task(self):
        person = self._current_person()
        if not person:
            QMessageBox.warning(self, "提示", "请先选择或创建人员")
            return
        content = self.task_content_edit.text().strip()
        if not content:
            QMessageBox.warning(self, "提示", "请输入任务内容")
            return
        status = self.task_status_combo.currentData()
        progress = self.task_progress_spin.value()
        date_str = self._current_date_str()

        person_data = self._task_data.setdefault(person, {})
        day_tasks = person_data.setdefault(date_str, [])

        if self.task_edit_id:
            for t in day_tasks:
                if t.get("id") == self.task_edit_id:
                    t["content"] = content
                    t["status"] = status
                    t["progress"] = progress
                    break
        else:
            day_tasks.append({
                "id": uuid.uuid4().hex[:8],
                "content": content,
                "status": status,
                "progress": progress,
                "note": "",
            })

        _save_tasks_data(self._task_data)
        self._cancel_task_edit()
        self.task_content_edit.clear()
        self.task_progress_spin.setValue(0)
        self._refresh_task_table()

    def _cancel_task_edit(self):
        self.task_edit_id = None
        self.task_content_edit.clear()
        self.task_status_combo.setCurrentIndex(0)
        self.task_progress_spin.setValue(0)
        self.task_cancel_btn.setEnabled(False)
        self._highlight_edit_row(None)

    def _delete_task(self, task_id: str):
        person = self._current_person()
        date_str = self._current_date_str()
        day_tasks = self._task_data.get(person, {}).get(date_str, [])
        for i, t in enumerate(day_tasks):
            if t.get("id") == task_id:
                day_tasks.pop(i)
                break
        _save_tasks_data(self._task_data)
        if self.task_edit_id == task_id:
            self._cancel_task_edit()
        self._refresh_task_table()

    def _set_task_status(self, task_id: str, status: str):
        person = self._current_person()
        date_str = self._current_date_str()
        for t in self._task_data.get(person, {}).get(date_str, []):
            if t.get("id") == task_id:
                t["status"] = status
                if status == TASK_STATUS_DONE:
                    t["progress"] = 100
                break
        _save_tasks_data(self._task_data)
        self._refresh_task_table()

    def _set_task_progress(self, task_id: str, progress: int):
        person = self._current_person()
        date_str = self._current_date_str()
        for t in self._task_data.get(person, {}).get(date_str, []):
            if t.get("id") == task_id:
                t["progress"] = progress
                if progress == 100:
                    t["status"] = TASK_STATUS_DONE
                elif progress > 0 and t["status"] == TASK_STATUS_TODO:
                    t["status"] = TASK_STATUS_DOING
                break
        _save_tasks_data(self._task_data)
        self._refresh_task_table()

    def _set_task_note(self, task_id: str, note: str):
        person = self._current_person()
        date_str = self._current_date_str()
        for t in self._task_data.get(person, {}).get(date_str, []):
            if t.get("id") == task_id:
                t["note"] = note
                break
        _save_tasks_data(self._task_data)

    # --- table rendering ---
    def _refresh_task_table(self):
        person = self._current_person()
        date_str = self._current_date_str()
        day_tasks = self._task_data.get(person, {}).get(date_str, [])

        self.task_table.setRowCount(0)
        for task in day_tasks:
            row = self.task_table.rowCount()
            self.task_table.insertRow(row)

            status = task.get("status", TASK_STATUS_TODO)
            if status == TASK_STATUS_DONE:
                check_item = QTableWidgetItem("✓")
                check_item.setTextAlignment(Qt.AlignCenter)
                check_item.setForeground(QColor(TASK_STATUS_COLORS[TASK_STATUS_DONE]))
                font = check_item.font()
                font.setBold(True)
                check_item.setFont(font)
                self.task_table.setItem(row, 0, check_item)

            content_item = QTableWidgetItem(task.get("content", ""))
            if status == TASK_STATUS_DONE:
                f = content_item.font()
                f.setStrikeOut(True)
                content_item.setFont(f)
                content_item.setForeground(QColor("#909399"))
            self.task_table.setItem(row, 1, content_item)

            status_btn = QPushButton(TASK_STATUS_LABELS.get(status, status))
            color = TASK_STATUS_COLORS.get(status, "#909399")
            status_btn.setStyleSheet(
                f"QPushButton {{ background-color: {color}; color: white; "
                f"padding: 3px 10px; border-radius: 3px; font-size: 12px; }}"
                f"QPushButton:hover {{ opacity: 0.9; }}"
            )
            status_btn.clicked.connect(lambda _=False, tid=task["id"]: self._cycle_task_status(tid))
            self.task_table.setCellWidget(row, 2, status_btn)

            progress = task.get("progress", 0)
            progress_widget = QWidget()
            prog_layout = QHBoxLayout(progress_widget)
            prog_layout.setContentsMargins(4, 2, 4, 2)
            prog_layout.setSpacing(4)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(progress)
            bar.setFormat("%v%")
            bar.setFixedHeight(16)
            bar.setStyleSheet(
                "QProgressBar { border: 1px solid #dcdfe6; border-radius: 3px; text-align: center; "
                "background: #f5f7fa; font-size: 11px; color: #606266; }"
                "QProgressBar::chunk { border-radius: 2px; }"
            )
            if progress >= 100:
                bar.setStyleSheet(
                    "QProgressBar { border: 1px solid #c2e7b0; border-radius: 3px; text-align: center; "
                    "background: #f0f9eb; font-size: 11px; color: #67c23a; }"
                    "QProgressBar::chunk { background-color: #67c23a; border-radius: 2px; }"
                )
            elif progress >= 50:
                bar.setStyleSheet(
                    "QProgressBar { border: 1px solid #fad79a; border-radius: 3px; text-align: center; "
                    "background: #fdf6ec; font-size: 11px; color: #e6a23c; }"
                    "QProgressBar::chunk { background-color: #e6a23c; border-radius: 2px; }"
                )
            minus_btn = QPushButton("-")
            minus_btn.setFixedSize(22, 20)
            minus_btn.setStyleSheet(
                "QPushButton { background-color: #f5f7fa; border: 1px solid #dcdfe6; "
                "border-radius: 3px; font-weight: bold; color: #606266; }"
                "QPushButton:hover { background-color: #e4e7ed; }"
            )
            minus_btn.clicked.connect(lambda _=False, tid=task["id"]: self._set_task_progress(tid, max(0, task.get("progress", 0) - 10)))
            plus_btn = QPushButton("+")
            plus_btn.setFixedSize(22, 20)
            plus_btn.setStyleSheet(
                "QPushButton { background-color: #f0f9eb; border: 1px solid #c2e7b0; "
                "border-radius: 3px; font-weight: bold; color: #67c23a; }"
                "QPushButton:hover { background-color: #e1f3d8; }"
            )
            plus_btn.clicked.connect(lambda _=False, tid=task["id"]: self._set_task_progress(tid, min(100, task.get("progress", 0) + 10)))
            prog_layout.addWidget(bar, 1)
            prog_layout.addWidget(minus_btn)
            prog_layout.addWidget(plus_btn)
            self.task_table.setCellWidget(row, 3, progress_widget)

            note_item = QTableWidgetItem(task.get("note", ""))
            note_item.setForeground(QColor("#606266"))
            self.task_table.setItem(row, 4, note_item)

            op_widget = QWidget()
            op_layout = QHBoxLayout(op_widget)
            op_layout.setContentsMargins(2, 2, 2, 2)
            op_layout.setSpacing(3)
            edit_btn = QPushButton("编辑")
            edit_btn.setFixedHeight(22)
            edit_btn.setStyleSheet(
                "QPushButton { background-color: #ecf5ff; color: #1976d2; border: 1px solid #b3d8ff; "
                "border-radius: 3px; padding: 0 8px; font-size: 11px; }"
                "QPushButton:hover { background-color: #d9ecff; }"
            )
            edit_btn.clicked.connect(lambda _=False, tid=task["id"]: self._enter_task_edit(tid))
            note_btn = QPushButton("备注")
            note_btn.setFixedHeight(22)
            note_btn.setStyleSheet(
                "QPushButton { background-color: #fdf6ec; color: #e6a23c; border: 1px solid #f5dab1; "
                "border-radius: 3px; padding: 0 8px; font-size: 11px; }"
                "QPushButton:hover { background-color: #faecd8; }"
            )
            note_btn.clicked.connect(lambda _=False, tid=task["id"]: self._edit_task_note(tid))
            del_btn = QPushButton("删除")
            del_btn.setFixedHeight(22)
            del_btn.setStyleSheet(
                "QPushButton { background-color: #fef0f0; color: #f56c6c; border: 1px solid #fbc4c4; "
                "border-radius: 3px; padding: 0 8px; font-size: 11px; }"
                "QPushButton:hover { background-color: #fde2e2; }"
            )
            del_btn.clicked.connect(lambda _=False, tid=task["id"]: self._delete_task(tid))
            op_layout.addWidget(edit_btn)
            op_layout.addWidget(note_btn)
            op_layout.addWidget(del_btn)
            self.task_table.setCellWidget(row, 5, op_widget)

        self._refresh_task_summary()

    def _refresh_task_summary(self):
        person = self._current_person()
        date_str = self._current_date_str()
        day_tasks = self._task_data.get(person, {}).get(date_str, [])
        total = len(day_tasks)
        if total == 0:
            self.task_summary_label.setText(f"📋 {date_str}　共 0 项任务")
            return
        done = sum(1 for t in day_tasks if t.get("status") == TASK_STATUS_DONE)
        doing = sum(1 for t in day_tasks if t.get("status") == TASK_STATUS_DOING)
        blocked = sum(1 for t in day_tasks if t.get("status") == TASK_STATUS_BLOCKED)
        avg_progress = round(sum(t.get("progress", 0) for t in day_tasks) / total)
        rate = round(done / total * 100)
        self.task_summary_label.setText(
            f"📋 {date_str}　共 {total} 项　✅ {done}　🚧 {doing}　⚠ {blocked}　"
            f"完成率 {rate}%　平均进度 {avg_progress}%"
        )

    def _cycle_task_status(self, task_id: str):
        order = [TASK_STATUS_TODO, TASK_STATUS_DOING, TASK_STATUS_DONE, TASK_STATUS_BLOCKED]
        person = self._current_person()
        date_str = self._current_date_str()
        day_tasks = self._task_data.get(person, {}).get(date_str, [])
        for t in day_tasks:
            if t.get("id") == task_id:
                cur = t.get("status", TASK_STATUS_TODO)
                idx = order.index(cur) if cur in order else 0
                nxt = order[(idx + 1) % len(order)]
                self._set_task_status(task_id, nxt)
                return

    def _enter_task_edit(self, task_id: str):
        person = self._current_person()
        date_str = self._current_date_str()
        for t in self._task_data.get(person, {}).get(date_str, []):
            if t.get("id") == task_id:
                self.task_edit_id = task_id
                self.task_content_edit.setText(t.get("content", ""))
                status = t.get("status", TASK_STATUS_TODO)
                idx = self.task_status_combo.findData(status)
                if idx >= 0:
                    self.task_status_combo.setCurrentIndex(idx)
                self.task_progress_spin.setValue(t.get("progress", 0))
                self.task_cancel_btn.setEnabled(True)
                self._highlight_edit_row(task_id)
                break

    def _highlight_edit_row(self, task_id):
        for row in range(self.task_table.rowCount()):
            item = self.task_table.item(row, 1)
            if not item:
                continue
            is_editing = False
            if task_id:
                w = self.task_table.cellWidget(row, 5)
                if w is not None:
                    for btn in w.findChildren(QPushButton):
                        if btn.text() == "编辑":
                            is_editing = True
                            break
            for col in range(self.task_table.columnCount()):
                it = self.task_table.item(row, col)
                if it:
                    it.setBackground(QColor("#fff7e6") if is_editing else QColor("transparent"))

    def _edit_task_note(self, task_id: str):
        person = self._current_person()
        date_str = self._current_date_str()
        day_tasks = self._task_data.get(person, {}).get(date_str, [])
        for t in day_tasks:
            if t.get("id") == task_id:
                note, ok = QInputDialog.getMultiLineText(
                    self, "编辑备注", "备注内容：", t.get("note", ""),
                )
                if ok:
                    self._set_task_note(task_id, note)
                    self._refresh_task_table()
                return

    def _on_task_double_clicked(self, index):
        row = index.row()
        person = self._current_person()
        date_str = self._current_date_str()
        day_tasks = self._task_data.get(person, {}).get(date_str, [])
        if 0 <= row < len(day_tasks):
            self._enter_task_edit(day_tasks[row]["id"])

    def _show_weekly_summary(self):
        person = self._current_person()
        today = self.task_date_edit.date()
        monday = today.addDays(-(today.dayOfWeek() - 1))
        lines = [f"📊 {person} 的本周任务汇总", f"周期：{monday.toString('yyyy-MM-dd')} ~ {today.toString('yyyy-MM-dd')}", ""]

        total_tasks = 0
        total_done = 0
        total_progress_sum = 0
        details = []
        for i in range(7):
            d = monday.addDays(i)
            ds = d.toString("yyyy-MM-dd")
            day_tasks = self._task_data.get(person, {}).get(ds, [])
            if not day_tasks:
                continue
            total_tasks += len(day_tasks)
            done = sum(1 for t in day_tasks if t.get("status") == TASK_STATUS_DONE)
            total_done += done
            avg_p = round(sum(t.get("progress", 0) for t in day_tasks) / len(day_tasks))
            total_progress_sum += avg_p * len(day_tasks)
            weekday = ["一", "二", "三", "四", "五", "六", "日"][i]
            details.append(f"  {ds}（周{weekday}）：{len(day_tasks)} 项　✅ {done}　平均进度 {avg_p}%")

        if total_tasks == 0:
            lines.append("本周暂无任务记录")
        else:
            lines.append(f"任务总数：{total_tasks}　已完成：{total_done}　"
                         f"完成率：{round(total_done / total_tasks * 100)}%　"
                         f"加权平均进度：{round(total_progress_sum / total_tasks)}%")
            lines.append("")
            lines.append("每日明细：")
            lines.extend(details)

        QMessageBox.information(self, "本周汇总", "\n".join(lines))

    def _export_today_tasks(self):
        person = self._current_person()
        date_str = self._current_date_str()
        day_tasks = self._task_data.get(person, {}).get(date_str, [])
        if not day_tasks:
            QMessageBox.information(self, "提示", "本日无任务可导出")
            return
        default_name = f"{person}_{date_str}_任务.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出为 CSV", default_name, "CSV 文件 (*.csv);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            import csv
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["序号", "任务内容", "状态", "进度(%)", "备注"])
                for i, t in enumerate(day_tasks, 1):
                    status_label = TASK_STATUS_LABELS.get(t.get("status", ""), "")
                    writer.writerow([
                        i, t.get("content", ""), status_label,
                        t.get("progress", 0), t.get("note", ""),
                    ])
            QMessageBox.information(self, "导出成功", f"已导出到：\n{path}")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))