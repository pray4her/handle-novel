from __future__ import annotations

import argparse
import os
from typing import Any, Dict


def _build_default_config() -> Dict[str, str]:
    return {
        "author_full_names_col": "Author Full Names",
        "reprint_col": "Reprint Addresses",
        "email_col": "Email Addresses",
        "wos_categories_col": "WoS Categories",
        "research_areas_col": "Research Areas",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="高性能期刊数据清洗与作者-邮箱对齐工具",
    )
    parser.add_argument(
        "--mode",
        choices=["gui", "cli"],
        default="gui",
        help="运行模式：gui 或 cli（默认 gui）",
    )
    parser.add_argument(
        "--input-dir",
        help="CLI 模式下的输入目录（包含 .xls/.xlsx 文件）",
    )
    parser.add_argument(
        "--db-path",
        default="journal_cleaner.db",
        help="SQLite 数据库路径（默认 journal_cleaner.db）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Worker 进程数量，0 表示自动选择",
    )

    args = parser.parse_args()

    if args.mode == "gui":
        from gui.app import run_gui

        run_gui()
        return

    # CLI 模式
    input_dir = args.input_dir
    if not input_dir:
        parser.error("--input-dir 在 cli 模式下为必填参数")

    if not os.path.isdir(input_dir):
        parser.error(f"输入目录不存在：{input_dir}")

    # 将 db 路径转换为绝对路径，避免多进程与打包后相对路径不一致
    db_path = args.db_path
    if not os.path.isabs(db_path):
        db_path = os.path.abspath(os.path.join(input_dir, db_path))

    from core.concurrency import run_etl

    config: Dict[str, str] = _build_default_config()

    def progress_callback(event: Dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "start":
            total = event.get("total_files")
            print(f"[START] 待处理文件数：{total}")
        elif etype == "file_progress":
            print(
                "[FILE]",
                event.get("file_name"),
                "完成：",
                f"{event.get('completed_files')}/{event.get('total_files')}，"
                f"成功 {event.get('success_rows')}，"
                f"跳过 {event.get('skipped_rows')}，"
                f"异常 {event.get('error_rows')}",
            )
        elif etype == "writer_done":
            total_records = event.get("total_records")
            total_errors = event.get("total_errors")
            writer_error = event.get("error")
            if writer_error:
                print(
                    "[WRITER_DONE]",
                    "记录数：",
                    total_records,
                    "错误记录：",
                    total_errors,
                    f"（写入错误：{writer_error}）",
                )
            else:
                print(
                    "[WRITER_DONE]",
                    "记录数：",
                    total_records,
                    "错误记录：",
                    total_errors,
                )
        elif etype == "finished":
            print(
                "[FINISHED]",
                "总文件数：",
                event.get("total_files"),
                "已处理：",
                event.get("completed_files"),
            )

    run_etl(
        input_dir=input_dir,
        db_path=db_path,
        config=config,
        num_workers=args.workers or None,
        progress_callback=progress_callback,
    )


if __name__ == "__main__":
    # 在 PyInstaller 打包后的环境中，多进程需要 freeze_support() 才能正常工作
    import multiprocessing as _mp

    _mp.freeze_support()
    main()


