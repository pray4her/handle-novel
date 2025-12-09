from __future__ import annotations

import os
import queue
import sys
import threading
import json
from typing import Any, Dict, List, Optional

import PyQt5

# 显式设置 Qt 平台插件目录，避免在裸环境中找不到 "windows" 插件。
# 若外部（如 PyInstaller）已设置该变量，则尊重外部配置，不再覆盖。
if "QT_QPA_PLATFORM_PLUGIN_PATH" not in os.environ:
    _qt_platforms_dir = os.path.join(
        os.path.dirname(PyQt5.__file__),
        "Qt5",
        "plugins",
        "platforms",
    )
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = _qt_platforms_dir

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QFrame,
    QCheckBox,
)

from core.concurrency import run_etl
from core.exporter import export_errors_to_excel, export_to_excel_by_tags, remove_duplicates
from core.email_validation import is_email_syntax_valid, validate_all_emails_in_db


class MainWindow(QMainWindow):
    """期刊数据清洗与作者-邮箱对齐 GUI。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("期刊数据清洗与作者-邮箱对齐工具")
        self.resize(960, 640)

        self._build_ui()
        self._apply_styles()
        # 加载上次运行保存的配置（输入目录、过滤条件等）
        self._load_settings()

        self.progress_events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.total_files = 0
        self.completed_files = 0
        self.total_success_rows = 0
        self.total_skipped_rows = 0
        self.total_error_rows = 0

        self.worker_thread: Optional[threading.Thread] = None
        self.export_thread: Optional[threading.Thread] = None
        self.validation_thread: Optional[threading.Thread] = None
        self.is_processing = False  # 标记是否正在处理
        self.is_exporting = False   # 标记是否正在导出
        self.is_validating = False  # 标记是否正在进行邮箱验证
        self.writer_done = False  # 标记写入是否完成

        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._drain_progress_events)
        self.timer.start()

    # ---------------- GUI 构建 ----------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # 顶部标题
        title = QLabel("期刊数据清洗与作者-邮箱对齐工具")
        title.setObjectName("AppTitle")
        subtitle = QLabel("批量处理 WoS Excel/CSV，自动解析通讯作者与邮箱并按学科标签导出")
        subtitle.setObjectName("AppSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

        # 上半部分：数据源与过滤配置
        top_layout = QHBoxLayout()
        top_layout.setSpacing(12)
        layout.addLayout(top_layout)

        # 左侧：数据源与列名
        data_group = QGroupBox("数据源与列配置")
        data_layout = QVBoxLayout(data_group)

        dir_layout = QHBoxLayout()
        self.input_dir_edit = QLineEdit()
        self.input_dir_edit.setPlaceholderText("选择包含 WoS 导出 Excel/CSV 的文件夹")
        browse_btn = QPushButton("选择输入目录")
        browse_btn.clicked.connect(self._on_browse_input_dir)
        dir_layout.addWidget(QLabel("输入目录："))
        dir_layout.addWidget(self.input_dir_edit)
        dir_layout.addWidget(browse_btn)
        data_layout.addLayout(dir_layout)

        db_layout = QHBoxLayout()
        self.db_path_edit = QLineEdit("journal_cleaner.db")
        self.db_path_edit.setPlaceholderText("清洗结果 SQLite 路径")
        db_browse_btn = QPushButton("选择/新建数据库")
        db_browse_btn.clicked.connect(self._on_browse_db_path)
        db_layout.addWidget(QLabel("SQLite 路径："))
        db_layout.addWidget(self.db_path_edit)
        db_layout.addWidget(db_browse_btn)
        data_layout.addLayout(db_layout)

        form = QFormLayout()
        self.col_author_full = QLineEdit("Author Full Names")
        self.col_reprint = QLineEdit("Reprint Addresses")
        self.col_email = QLineEdit("Email Addresses")
        self.col_wos = QLineEdit("WoS Categories")
        self.col_research = QLineEdit("Research Areas")

        form.addRow("Author Full Names 列名：", self.col_author_full)
        form.addRow("Reprint Addresses 列名：", self.col_reprint)
        form.addRow("Email Addresses 列名：", self.col_email)
        form.addRow("WoS Categories 列名：", self.col_wos)
        form.addRow("Research Areas 列名：", self.col_research)
        data_layout.addLayout(form)

        top_layout.addWidget(data_group, 2)

        # 右侧：过滤与运行参数
        filter_group = QGroupBox("过滤条件与运行参数")
        filter_layout = QVBoxLayout(filter_group)

        # 过滤条件
        filter_form = QFormLayout()

        # WoS 过滤
        self.wos_keywords_edit = QLineEdit("")
        self.wos_keywords_edit.setPlaceholderText("示例：Business; Psychology, Applied")
        wos_row = QHBoxLayout()
        wos_row.addWidget(self.wos_keywords_edit)
        wos_clear_btn = QPushButton("清空")
        wos_clear_btn.setFixedWidth(48)
        wos_clear_btn.clicked.connect(self.wos_keywords_edit.clear)
        wos_row.addWidget(wos_clear_btn)
        filter_form.addRow("WoS 过滤关键词（分号分隔）：", wos_row)

        # Research Areas 过滤
        self.research_keywords_edit = QLineEdit("")
        self.research_keywords_edit.setPlaceholderText("示例：Management; Economics")
        research_row = QHBoxLayout()
        research_row.addWidget(self.research_keywords_edit)
        research_clear_btn = QPushButton("清空")
        research_clear_btn.setFixedWidth(48)
        research_clear_btn.clicked.connect(self.research_keywords_edit.clear)
        research_row.addWidget(research_clear_btn)
        filter_form.addRow("Research Areas 过滤关键词：", research_row)

        # 原始文件名过滤
        self.file_name_filter_edit = QLineEdit("")
        self.file_name_filter_edit.setPlaceholderText("按原始文件名过滤，分号分隔，多值为包含任一即可")
        file_row = QHBoxLayout()
        file_row.addWidget(self.file_name_filter_edit)
        file_clear_btn = QPushButton("清空")
        file_clear_btn.setFixedWidth(48)
        file_clear_btn.clicked.connect(self.file_name_filter_edit.clear)
        file_row.addWidget(file_clear_btn)
        filter_form.addRow("原始文件名过滤（分号分隔）：", file_row)

        # 国家过滤
        self.country_filter_edit = QLineEdit("")
        self.country_filter_edit.setPlaceholderText("示例：USA; Canada")
        country_row = QHBoxLayout()
        country_row.addWidget(self.country_filter_edit)
        country_clear_btn = QPushButton("清空")
        country_clear_btn.setFixedWidth(48)
        country_clear_btn.clicked.connect(self.country_filter_edit.clear)
        country_row.addWidget(country_clear_btn)
        filter_form.addRow("国家过滤（分号分隔）：", country_row)

        # 华人华裔过滤
        self.ethnic_filter_edit = QLineEdit("")
        self.ethnic_filter_edit.setPlaceholderText("示例：国内华人; 海外华人; 外国人")
        ethnic_row = QHBoxLayout()
        ethnic_row.addWidget(self.ethnic_filter_edit)
        ethnic_clear_btn = QPushButton("清空")
        ethnic_clear_btn.setFixedWidth(48)
        ethnic_clear_btn.clicked.connect(self.ethnic_filter_edit.clear)
        ethnic_row.addWidget(ethnic_clear_btn)
        filter_form.addRow("华人华裔过滤（分号分隔）：", ethnic_row)

        filter_layout.addLayout(filter_form)

        # 导出列选项
        export_cols_group = QGroupBox("导出列选项")
        export_cols_layout = QHBoxLayout(export_cols_group)

        self.include_file_name_cb = QCheckBox("原始文件名")
        self.include_file_name_cb.setChecked(True)
        export_cols_layout.addWidget(self.include_file_name_cb)

        self.include_country_cb = QCheckBox("国家")
        self.include_country_cb.setChecked(True)
        export_cols_layout.addWidget(self.include_country_cb)

        self.include_ethnic_cb = QCheckBox("华人华裔")
        self.include_ethnic_cb.setChecked(True)
        export_cols_layout.addWidget(self.include_ethnic_cb)

        export_cols_layout.addStretch(1)
        filter_layout.addWidget(export_cols_group)

        # 邮箱验证参数
        email_group = QGroupBox("邮箱验证参数")
        email_form = QFormLayout(email_group)

        self.smtp_from_edit = QLineEdit("")
        self.smtp_from_edit.setPlaceholderText("用于验证的发件邮箱（不会发送正文）")
        email_form.addRow("发件邮箱（MAIL FROM）：", self.smtp_from_edit)

        self.mx_timeout_spin = QSpinBox()
        self.mx_timeout_spin.setRange(1, 30)
        self.mx_timeout_spin.setValue(3)
        email_form.addRow("MX 查询超时（秒）：", self.mx_timeout_spin)

        self.smtp_timeout_spin = QSpinBox()
        self.smtp_timeout_spin.setRange(5, 120)
        self.smtp_timeout_spin.setValue(15)
        email_form.addRow("SMTP 超时（秒）：", self.smtp_timeout_spin)

        self.smtp_concurrency_spin = QSpinBox()
        self.smtp_concurrency_spin.setRange(1, 50)
        self.smtp_concurrency_spin.setValue(10)
        email_form.addRow("验证并发数量：", self.smtp_concurrency_spin)

        self.smtp_attempts_spin = QSpinBox()
        # 这里的数值含义调整为：在单次“验证邮箱有效性”任务中，
        # 对每个邮箱在得到“不可验证”结果时，最多再自动重试的次数。
        self.smtp_attempts_spin.setRange(0, 10)
        self.smtp_attempts_spin.setValue(3)
        email_form.addRow("每个邮箱自动重试次数：", self.smtp_attempts_spin)

        filter_layout.addWidget(email_group)

        ctrl_layout = QHBoxLayout()
        ctrl_layout.addWidget(QLabel("Worker 数量："))
        self.worker_spin = QSpinBox()
        self.worker_spin.setMinimum(1)
        try:
            import multiprocessing as mp

            max_workers = max(mp.cpu_count() or 4, 4)
        except Exception:
            max_workers = 4
        self.worker_spin.setMaximum(max_workers)
        self.worker_spin.setValue(min(4, max_workers))
        ctrl_layout.addWidget(self.worker_spin)

        ctrl_layout.addWidget(QLabel("导出块大小："))
        self.chunk_spin = QSpinBox()
        self.chunk_spin.setRange(1000, 1_000_000)
        self.chunk_spin.setSingleStep(1000)
        self.chunk_spin.setValue(10_000)
        ctrl_layout.addWidget(self.chunk_spin)

        ctrl_layout.addStretch(1)

        self.start_btn = QPushButton("开始处理")
        self.start_btn.setObjectName("PrimaryButton")
        self.start_btn.clicked.connect(self._on_start_etl)
        ctrl_layout.addWidget(self.start_btn)

        self.export_btn = QPushButton("导出数据")
        self.export_btn.setObjectName("SecondaryButton")
        self.export_btn.clicked.connect(self._on_export)
        ctrl_layout.addWidget(self.export_btn)

        self.export_errors_btn = QPushButton("导出错误/跳过")
        self.export_errors_btn.clicked.connect(self._on_export_errors)
        ctrl_layout.addWidget(self.export_errors_btn)

        self.dedup_btn = QPushButton("数据库去重")
        self.dedup_btn.clicked.connect(self._on_deduplicate)
        ctrl_layout.addWidget(self.dedup_btn)

        # 按钮功能：对数据库中“未验证或当前标记为不可验证”的邮箱，
        # 进行批量验证，并按照上方的“自动重试次数”参数，在单次任务内自动重试。
        self.validate_email_btn = QPushButton("验证邮箱有效性（含自动重试）")
        self.validate_email_btn.setToolTip(
            "对数据库中邮箱有效性进行批量验证：\n"
            " - 仅处理 email_validity 为空或为“不可验证”的记录；\n"
            " - 本次任务内，每个邮箱在得到“不可验证”结果时，"
            "会按上方设置的次数自动重试。"
        )
        self.validate_email_btn.clicked.connect(self._on_validate_emails)
        ctrl_layout.addWidget(self.validate_email_btn)

        filter_layout.addLayout(ctrl_layout)

        top_layout.addWidget(filter_group, 3)

        # 下半部分：进度与日志
        bottom_group = QGroupBox("进度与运行日志")
        bottom_layout = QVBoxLayout(bottom_group)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        bottom_layout.addWidget(self.progress_bar)

        stats_layout = QHBoxLayout()
        self.success_label = QLabel("成功条数：0")
        self.skipped_label = QLabel("跳过条数：0")
        self.error_label = QLabel("异常条数：0")
        stats_layout.addWidget(self.success_label)
        stats_layout.addWidget(self.skipped_label)
        stats_layout.addWidget(self.error_label)
        stats_layout.addStretch(1)
        bottom_layout.addLayout(stats_layout)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setPlaceholderText("这里将显示处理进度与错误原因的摘要…")
        bottom_layout.addWidget(self.log_edit)

        layout.addWidget(bottom_group, 1)

    def _apply_styles(self) -> None:
        """统一应用一套较为现代的浅色主题样式。"""
        self.setStyleSheet(
            """
            QMainWindow {
                background: #f5f5f7;
            }
            #AppTitle {
                font-size: 20px;
                font-weight: 600;
            }
            #AppSubtitle {
                color: #666666;
                font-size: 11px;
            }
            QGroupBox {
                border: 1px solid #d0d0d0;
                border-radius: 6px;
                margin-top: 8px;
                background: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
                color: #333333;
                font-weight: 500;
            }
            QLabel {
                color: #333333;
            }
            QLineEdit, QTextEdit, QSpinBox {
                background: #ffffff;
                border: 1px solid #cccccc;
                border-radius: 4px;
                padding: 2px 4px;
            }
            QLineEdit:focus, QTextEdit:focus, QSpinBox:focus {
                border: 1px solid #3478f6;
            }
            QPushButton {
                min-width: 80px;
                padding: 6px 14px;
                border-radius: 4px;
                border: 1px solid #3478f6;
                background-color: #ffffff;
                color: #3478f6;
            }
            QPushButton#PrimaryButton {
                background-color: #3478f6;
                color: #ffffff;
            }
            QPushButton#PrimaryButton:hover {
                background-color: #245fcc;
            }
            QPushButton#SecondaryButton:hover {
                background-color: #e6f0ff;
            }
            QProgressBar {
                border: 1px solid #cccccc;
                border-radius: 4px;
                text-align: center;
                height: 16px;
            }
            QProgressBar::chunk {
                background-color: #3478f6;
                border-radius: 3px;
            }
            """
        )

    # ---------------- 过滤条件与配置持久化 ----------------

    def _parse_semicolon_list(self, text: str) -> Optional[List[str]]:
        parts = [s.strip() for s in str(text).split(";") if s.strip()]
        return parts or None

    def _collect_filters(self) -> Dict[str, Optional[List[str]]]:
        return {
            "wos_keywords": self._parse_semicolon_list(self.wos_keywords_edit.text().strip()),
            "research_keywords": self._parse_semicolon_list(self.research_keywords_edit.text().strip()),
            "file_name_keywords": self._parse_semicolon_list(self.file_name_filter_edit.text().strip()),
            "country_keywords": self._parse_semicolon_list(self.country_filter_edit.text().strip()),
            "ethnic_filters": self._parse_semicolon_list(self.ethnic_filter_edit.text().strip()),
        }

    def _settings_path(self) -> str:
        home = os.path.expanduser("~")
        return os.path.join(home, ".journal_cleaner_gui.json")

    def _load_settings(self) -> None:
        path = self._settings_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        # 基本路径
        self.input_dir_edit.setText(str(data.get("input_dir", "")))
        self.db_path_edit.setText(str(data.get("db_path", self.db_path_edit.text())))

        # 列名与过滤条件
        self.col_author_full.setText(str(data.get("col_author_full", self.col_author_full.text())))
        self.col_reprint.setText(str(data.get("col_reprint", self.col_reprint.text())))
        self.col_email.setText(str(data.get("col_email", self.col_email.text())))
        self.col_wos.setText(str(data.get("col_wos", self.col_wos.text())))
        self.col_research.setText(str(data.get("col_research", self.col_research.text())))

        self.wos_keywords_edit.setText(str(data.get("wos_keywords", "")))
        self.research_keywords_edit.setText(str(data.get("research_keywords", "")))
        self.file_name_filter_edit.setText(str(data.get("file_name_filter", "")))
        self.country_filter_edit.setText(str(data.get("country_filter", "")))
        self.ethnic_filter_edit.setText(str(data.get("ethnic_filter", "")))

        # 并发与块大小
        self.worker_spin.setValue(int(data.get("worker_count", self.worker_spin.value())))
        self.chunk_spin.setValue(int(data.get("chunk_size", self.chunk_spin.value())))

        # 邮箱验证相关
        self.smtp_from_edit.setText(str(data.get("smtp_from", "")))
        self.mx_timeout_spin.setValue(int(data.get("mx_timeout", self.mx_timeout_spin.value())))
        self.smtp_timeout_spin.setValue(int(data.get("smtp_timeout", self.smtp_timeout_spin.value())))
        self.smtp_concurrency_spin.setValue(int(data.get("smtp_concurrency", self.smtp_concurrency_spin.value())))
        self.smtp_attempts_spin.setValue(int(data.get("smtp_retry_times", self.smtp_attempts_spin.value())))

        # 导出列选项
        self.include_file_name_cb.setChecked(bool(data.get("include_file_name", True)))
        self.include_country_cb.setChecked(bool(data.get("include_country", True)))
        self.include_ethnic_cb.setChecked(bool(data.get("include_ethnic", True)))

    def _save_settings(self) -> None:
        data: Dict[str, Any] = {
            "input_dir": self.input_dir_edit.text().strip(),
            "db_path": self.db_path_edit.text().strip(),
            "col_author_full": self.col_author_full.text().strip(),
            "col_reprint": self.col_reprint.text().strip(),
            "col_email": self.col_email.text().strip(),
            "col_wos": self.col_wos.text().strip(),
            "col_research": self.col_research.text().strip(),
            "wos_keywords": self.wos_keywords_edit.text().strip(),
            "research_keywords": self.research_keywords_edit.text().strip(),
            "file_name_filter": self.file_name_filter_edit.text().strip(),
            "country_filter": self.country_filter_edit.text().strip(),
            "ethnic_filter": self.ethnic_filter_edit.text().strip(),
            "worker_count": self.worker_spin.value(),
            "chunk_size": self.chunk_spin.value(),
            "smtp_from": self.smtp_from_edit.text().strip(),
            "mx_timeout": self.mx_timeout_spin.value(),
            "smtp_timeout": self.smtp_timeout_spin.value(),
            "smtp_concurrency": self.smtp_concurrency_spin.value(),
            "smtp_retry_times": self.smtp_attempts_spin.value(),
            "include_file_name": self.include_file_name_cb.isChecked(),
            "include_country": self.include_country_cb.isChecked(),
            "include_ethnic": self.include_ethnic_cb.isChecked(),
        }
        path = self._settings_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            # 配置保存失败不应影响主流程
            pass

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self._save_settings()
        finally:
            super().closeEvent(event)

    # ---------------- 事件处理 ----------------

    def _append_log(self, text: str) -> None:
        self.log_edit.append(text)

    def _on_browse_input_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择输入目录")
        if directory:
            self.input_dir_edit.setText(directory)

    def _on_browse_db_path(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "选择或新建 SQLite 数据库文件",
            self.db_path_edit.text(),
            "SQLite Database (*.db);;All Files (*)"
        )
        if file_path:
            self.db_path_edit.setText(file_path)

    def _on_start_etl(self) -> None:
        input_dir = self.input_dir_edit.text().strip()
        if not input_dir:
            self._append_log("请先选择输入目录。")
            return
        if not os.path.isdir(input_dir):
            self._append_log(f"目录不存在：{input_dir}")
            return

        db_path = self.db_path_edit.text().strip() or "journal_cleaner.db"
        # 相对路径时默认生成在输入目录下，并转成绝对路径，便于在打包后环境中定位
        if not os.path.isabs(db_path):
            base = input_dir or os.getcwd()
            db_path = os.path.abspath(os.path.join(base, db_path))
            self.db_path_edit.setText(db_path)
        config = {
            "author_full_names_col": self.col_author_full.text().strip(),
            "reprint_col": self.col_reprint.text().strip(),
            "email_col": self.col_email.text().strip(),
            "wos_categories_col": self.col_wos.text().strip(),
            "research_areas_col": self.col_research.text().strip(),
        }
        num_workers = self.worker_spin.value()

        # 重置统计与进度
        self.total_files = 0
        self.completed_files = 0
        self.total_success_rows = 0
        self.total_skipped_rows = 0
        self.total_error_rows = 0
        self.writer_done = False
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(1)
        self.success_label.setText("成功条数：0")
        self.skipped_label.setText("跳过条数：0")
        self.error_label.setText("异常条数：0")
        self.log_edit.clear()

        self.start_btn.setEnabled(False)
        self.is_processing = True
        self.writer_done = False
        self._append_log("开始处理...")

        def progress_callback(event: Dict[str, Any]) -> None:
            self.progress_events.put(event)

        def worker_target() -> None:
            try:
                run_etl(input_dir, db_path, config, num_workers, progress_callback)
            except Exception as e:
                self.progress_events.put({"type": "error", "message": str(e)})
            finally:
                self.progress_events.put({"type": "thread_finished"})

        self.worker_thread = threading.Thread(target=worker_target, daemon=True)
        self.worker_thread.start()

    def _on_export(self) -> None:
        # 检查是否正在处理
        if self.is_processing and not self.writer_done:
            self._append_log("警告：数据处理尚未完成，导出可能包含不完整数据。请等待处理完成后再导出。")
            reply = QMessageBox.warning(
                self,
                "确认导出",
                "数据处理尚未完成，导出可能包含不完整数据。\n\n是否继续导出？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
        
        if self.is_exporting:
             self._append_log("正在进行导出任务，请稍候...")
             return

        db_path = self.db_path_edit.text().strip() or "journal_cleaner.db"
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)
            self.db_path_edit.setText(db_path)
        if not os.path.exists(db_path):
            self._append_log(f"SQLite 文件不存在：{db_path}")
            return

        output_dir = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not output_dir:
            return

        wos_raw = self.wos_keywords_edit.text().strip()
        # 收集统一的过滤条件（WoS / Research / 原始文件名 / 国家 / 华人华裔）
        filters = self._collect_filters()
        wos_keywords = filters["wos_keywords"]
        research_keywords = filters["research_keywords"]
        file_name_keywords = filters["file_name_keywords"]
        country_keywords = filters["country_keywords"]
        ethnic_filters = filters["ethnic_filters"]

        chunk_size = self.chunk_spin.value()

        include_file_name = self.include_file_name_cb.isChecked()
        include_country = self.include_country_cb.isChecked()
        include_ethnic = self.include_ethnic_cb.isChecked()

        # 启动导出线程
        def export_target():
            try:
                self.progress_events.put({"type": "export_start", "message": "正在导出清洗结果数据..."})
                files = export_to_excel_by_tags(
                    db_path,
                    output_dir,
                    wos_keywords=wos_keywords,
                    research_keywords=research_keywords,
                    file_name_keywords=file_name_keywords,
                    country_keywords=country_keywords,
                    ethnic_filters=ethnic_filters,
                    chunk_size=chunk_size,
                    include_file_name=include_file_name,
                    include_country=include_country,
                    include_ethnic_chinese=include_ethnic,
                )
                self.progress_events.put({"type": "export_done", "files": files, "title": "数据导出"})
            except Exception as e:
                self.progress_events.put({"type": "export_error", "message": str(e)})

        self.export_thread = threading.Thread(target=export_target, daemon=True)
        self.export_thread.start()

    def _on_export_errors(self) -> None:
        # 检查是否正在处理
        if self.is_processing and not self.writer_done:
            self._append_log("警告：数据处理尚未完成，导出可能包含不完整数据。请等待处理完成后再导出。")
            reply = QMessageBox.warning(
                self,
                "确认导出",
                "数据处理尚未完成，导出可能包含不完整数据。\n\n是否继续导出？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
        
        if self.is_exporting:
             self._append_log("正在进行导出任务，请稍候...")
             return

        db_path = self.db_path_edit.text().strip() or "journal_cleaner.db"
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)
            self.db_path_edit.setText(db_path)
        if not os.path.exists(db_path):
            self._append_log(f"SQLite 文件不存在：{db_path}")
            return

        output_dir = QFileDialog.getExistingDirectory(self, "选择错误/跳过数据导出目录")
        if not output_dir:
            return

        chunk_size = self.chunk_spin.value()

        # 启动导出线程
        def export_target():
            try:
                self.progress_events.put({"type": "export_start", "message": "正在导出异常数据..."})
                files = export_errors_to_excel(
                    db_path,
                    output_dir,
                    chunk_size=chunk_size,
                )
                self.progress_events.put({"type": "export_done", "files": files, "title": "异常数据导出"})
            except Exception as e:
                self.progress_events.put({"type": "export_error", "message": str(e)})

        self.export_thread = threading.Thread(target=export_target, daemon=True)
        self.export_thread.start()

    def _on_deduplicate(self) -> None:
        # 检查是否正在处理
        if self.is_processing and not self.writer_done:
            self._append_log("警告：数据处理尚未完成，去重可能导致数据不一致。")
            reply = QMessageBox.warning(
                self,
                "确认去重",
                "数据处理尚未完成，建议等待处理完成后再去重。\n\n是否强行继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return

        if self.is_exporting:
             self._append_log("正在进行导出/去重任务，请稍候...")
             return

        db_path = self.db_path_edit.text().strip() or "journal_cleaner.db"
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)
            self.db_path_edit.setText(db_path)
        if not os.path.exists(db_path):
            self._append_log(f"SQLite 文件不存在：{db_path}")
            return
            
        reply = QMessageBox.question(
            self,
            "确认去重",
            "将基于 (short_name, email) 对数据库进行去重，保留 id 最小的记录，删除重复项。\n此操作不可逆！\n\n是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        def dedup_target():
            try:
                self.progress_events.put({"type": "export_start", "message": "正在执行数据库去重..."})
                removed_count = remove_duplicates(db_path)
                self.progress_events.put({"type": "log", "message": f"数据库去重完成，共删除了 {removed_count} 条重复记录。"})
                self.progress_events.put({"type": "dedup_done"})
            except Exception as e:
                self.progress_events.put({"type": "export_error", "message": str(e)})

        self.export_thread = threading.Thread(target=dedup_target, daemon=True)
        self.export_thread.start()

    def _on_validate_emails(self) -> None:
        # 检查数据库路径
        db_path = self.db_path_edit.text().strip() or "journal_cleaner.db"
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)
            self.db_path_edit.setText(db_path)
        if not os.path.exists(db_path):
            self._append_log(f"SQLite 文件不存在：{db_path}")
            return

        # 检查发件邮箱
        mail_from = self.smtp_from_edit.text().strip()
        if not mail_from:
            self._append_log("请先填写用于验证的发件邮箱。")
            return
        ok, reason = is_email_syntax_valid(mail_from)
        if not ok:
            self._append_log(f"发件邮箱格式不合法：{reason}")
            return

        # 若正在进行 ETL 或导出/去重，提示用户
        if (self.is_processing and not self.writer_done) or self.is_exporting:
            msg = (
                "当前仍有任务在运行（清洗/导出/去重）。"
                "\n建议等待任务完成后再进行邮箱验证，以避免数据库锁竞争。"
                "\n\n是否仍要继续执行邮箱验证？"
            )
            reply = QMessageBox.warning(
                self,
                "确认邮箱验证",
                msg,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        if self.is_validating:
            self._append_log("邮箱验证任务正在进行中，请稍候…")
            return

        mx_timeout = float(self.mx_timeout_spin.value())
        smtp_timeout = float(self.smtp_timeout_spin.value())
        concurrency = int(self.smtp_concurrency_spin.value())
        max_retries = 2
        # 单次任务中，当某个邮箱得到“不可验证”结果时，额外自动重试的次数
        retry_times = int(self.smtp_attempts_spin.value())

        # 与导出共享的过滤条件
        filters = self._collect_filters()
        wos_keywords = filters["wos_keywords"]
        research_keywords = filters["research_keywords"]
        file_name_keywords = filters["file_name_keywords"]
        country_keywords = filters["country_keywords"]
        ethnic_filters = filters["ethnic_filters"]

        self.is_validating = True
        self.validate_email_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.export_errors_btn.setEnabled(False)
        self.dedup_btn.setEnabled(False)

        self._append_log("开始邮箱有效性批量验证…")
        if any(filters.values()):
            self._append_log(
                "本次邮箱验证使用的过滤条件："
                f" WoS={wos_keywords or '全部'},"
                f" Research={research_keywords or '全部'},"
                f" 原始文件名={file_name_keywords or '全部'},"
                f" 国家={country_keywords or '全部'},"
                f" 华人华裔={ethnic_filters or '全部'}"
            )

        def progress_callback(event: Dict[str, Any]) -> None:
            self.progress_events.put(event)

        def validate_target() -> None:
            try:
                validate_all_emails_in_db(
                    db_path=db_path,
                    mail_from=mail_from,
                    mx_timeout=mx_timeout,
                    smtp_timeout=smtp_timeout,
                    concurrency=concurrency,
                    max_retries=max_retries,
                    retry_times=retry_times,
                    wos_keywords=wos_keywords,
                    research_keywords=research_keywords,
                    file_name_keywords=file_name_keywords,
                    country_keywords=country_keywords,
                    ethnic_filters=ethnic_filters,
                    progress_callback=progress_callback,
                )
            except Exception as e:
                self.progress_events.put(
                    {
                        "type": "email_validation_error",
                        "message": str(e),
                    }
                )
            finally:
                self.progress_events.put({"type": "email_validation_thread_finished"})

        self.validation_thread = threading.Thread(
            target=validate_target, daemon=True
        )
        self.validation_thread.start()

    # ---------------- 进度消费 ----------------

    def _drain_progress_events(self) -> None:
        while True:
            try:
                event = self.progress_events.get_nowait()
            except queue.Empty:
                break

            etype = event.get("type")

            if etype == "start":
                self.total_files = int(event.get("total_files") or 0)
                self.progress_bar.setMaximum(max(self.total_files, 1))
                self._append_log(f"待处理文件数：{self.total_files}")
            elif etype == "file_progress":
                self.completed_files = int(
                    event.get("completed_files") or self.completed_files
                )
                succ = int(event.get("success_rows") or 0)
                skip = int(event.get("skipped_rows") or 0)
                err = int(event.get("error_rows") or 0)

                self.total_success_rows += succ
                self.total_skipped_rows += skip
                self.total_error_rows += err

                self.progress_bar.setValue(self.completed_files)
                self.success_label.setText(f"成功条数：{self.total_success_rows}")
                self.skipped_label.setText(f"跳过条数：{self.total_skipped_rows}")
                self.error_label.setText(f"异常条数：{self.total_error_rows}")

                file_name = event.get("file_name")
                self._append_log(
                    f"文件完成：{file_name} | 成功 {succ} 跳过 {skip} 异常 {err}"
                )
            elif etype == "writer_done":
                total_records = int(event.get("total_records") or 0)
                total_errors = int(event.get("total_errors") or 0)
                writer_error = event.get("error")
                self.writer_done = True  # 标记写入已完成
                if writer_error:
                    self._append_log(
                        f"写入完成（有错误），记录数：{total_records}，错误记录：{total_errors}"
                    )
                    self._append_log(f"写入错误：{writer_error}")
                else:
                    self._append_log(
                        f"写入完成，记录数：{total_records}，错误记录：{total_errors}"
                    )
            elif etype == "finished":
                self._append_log("全部处理完成。")
                self.is_processing = False
                self.start_btn.setEnabled(True)
            elif etype == "error":
                self._append_log(f"后台错误：{event.get('message')}")
                self.is_processing = False
            elif etype == "thread_finished":
                # 保底启用按钮和重置状态
                self.is_processing = False
                self.start_btn.setEnabled(True)
            elif etype == "export_start":
                self.is_exporting = True
                self.export_btn.setEnabled(False)
                self.export_errors_btn.setEnabled(False)
                self.dedup_btn.setEnabled(False)
                self._append_log(f"任务开始：{event.get('message')}")
            elif etype == "export_done":
                self.is_exporting = False
                self.export_btn.setEnabled(True)
                self.export_errors_btn.setEnabled(True)
                self.dedup_btn.setEnabled(True)
                files = event.get("files") or []
                if not files:
                    self._append_log(f"{event.get('title', '导出')}：没有产生文件。")
                else:
                    self._append_log(f"{event.get('title', '导出')}完成：")
                    for p in files:
                        self._append_log(f"  {p}")
            elif etype == "export_error":
                self.is_exporting = False
                self.export_btn.setEnabled(True)
                self.export_errors_btn.setEnabled(True)
                self.dedup_btn.setEnabled(True)
                self._append_log(f"任务出错：{event.get('message')}")
            elif etype == "log":
                self._append_log(str(event.get("message")))
            elif etype == "dedup_done":
                self.is_exporting = False
                self.export_btn.setEnabled(True)
                self.export_errors_btn.setEnabled(True)
                self.dedup_btn.setEnabled(True)
                self._append_log("去重操作已完成。")
            elif etype == "email_validation_start":
                total = int(event.get("total_emails") or 0)
                self.progress_bar.setMaximum(max(total, 1))
                self.progress_bar.setValue(0)
                self._append_log(f"开始邮箱验证，待验证邮箱数：{total}")
            elif etype == "email_validation_progress":
                completed = int(event.get("completed") or 0)
                total = int(event.get("total") or 0)
                valid = int(event.get("valid") or 0)
                invalid = int(event.get("invalid") or 0)
                unknown = int(event.get("unknown") or 0)
                self.progress_bar.setMaximum(max(total, 1))
                self.progress_bar.setValue(completed)
                # 为避免刷屏，只在特定步长时输出摘要
                if completed == total or completed % 50 == 0:
                    self._append_log(
                        f"邮箱验证进度：{completed}/{total}，"
                        f"有效 {valid}，无效 {invalid}，不可验证 {unknown}"
                    )
            elif etype == "email_validation_done":
                total = int(event.get("total") or 0)
                valid = int(event.get("valid") or 0)
                invalid = int(event.get("invalid") or 0)
                unknown = int(event.get("unknown") or 0)
                self._append_log(
                    f"邮箱验证完成，总数 {total}：有效 {valid}，无效 {invalid}，不可验证 {unknown}。"
                )
                self.is_validating = False
                self.validate_email_btn.setEnabled(True)
                # 其他按钮在 thread_finished 中兜底恢复
            elif etype == "email_validation_error":
                self._append_log(f"邮箱验证任务出错：{event.get('message')}")
                self.is_validating = False
                self.validate_email_btn.setEnabled(True)
                self.start_btn.setEnabled(True)
                self.export_btn.setEnabled(True)
                self.export_errors_btn.setEnabled(True)
                self.dedup_btn.setEnabled(True)
            elif etype == "email_validation_thread_finished":
                # 兜底，确保状态恢复
                self.is_validating = False
                self.validate_email_btn.setEnabled(True)
                if not self.is_processing:
                    self.start_btn.setEnabled(True)
                if not self.is_exporting:
                    self.export_btn.setEnabled(True)
                    self.export_errors_btn.setEnabled(True)
                    self.dedup_btn.setEnabled(True)


def run_gui() -> None:
    """运行 GUI 应用。"""
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


__all__ = ["MainWindow", "run_gui"]


