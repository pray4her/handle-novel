from __future__ import annotations

import multiprocessing as mp
import os
import queue
import sqlite3
from typing import Any, Callable, Dict, Iterable, List, Tuple

from .etl_worker import ensure_default_config, process_file


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cur.fetchall()]
    if column not in columns:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


def _ensure_db_schema(conn: sqlite3.Connection) -> None:
    """创建 records / errors 表（若不存在），并确保新列存在。"""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT,
            row_index INTEGER,
            short_name TEXT,
            country TEXT,
            full_name TEXT,
            email TEXT,
            wos_categories TEXT,
            research_areas TEXT,
            raw_reprint TEXT,
            raw_email TEXT,
            email_validity TEXT,
            email_validation_attempts INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT,
            row_index INTEGER,
            reason TEXT,
            raw_reprint TEXT,
            raw_email TEXT,
            raw_row TEXT
        )
        """
    )
    # 创建索引以加速查询
    cur.execute("CREATE INDEX IF NOT EXISTS idx_records_wos ON records(wos_categories)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_records_research ON records(research_areas)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_records_filename ON records(file_name)")
    
    conn.commit()
    # 确保新增列在旧数据库上也存在
    _ensure_column(conn, "records", "country", "TEXT")
    _ensure_column(conn, "records", "email_validity", "TEXT")
    _ensure_column(conn, "records", "email_validation_attempts", "INTEGER")
    _ensure_column(conn, "errors", "raw_row", "TEXT")


def _writer_process(
    db_path: str,
    result_queue: "mp.Queue[Tuple[str, Any]]",
    progress_queue: "mp.Queue[Tuple[Any, ...]]",
    num_workers: int,
    commit_batch_size: int = 1000,
) -> None:
    """
    Writer 进程：唯一负责写入 SQLite，避免多进程锁竞争。
    """
    conn: sqlite3.Connection | None = None
    cur: sqlite3.Cursor | None = None
    # 需要在 try 之外初始化，便于在异常/finally 分支安全访问
    records_buffer: List[Tuple[Any, ...]] = []
    errors_buffer: List[Tuple[Any, ...]] = []
    total_records = 0
    total_errors = 0
    writer_error: str | None = None

    try:
        conn = sqlite3.connect(db_path)
        _ensure_db_schema(conn)
        cur = conn.cursor()

        done_workers = 0

        while done_workers < num_workers:
            msg = result_queue.get()
            if not msg:
                continue

            typ = msg[0]
            if typ == "records":
                batch = msg[1]
                if not batch:
                    continue
                records_buffer.extend(batch)
                total_records += len(batch)
                if len(records_buffer) >= commit_batch_size:
                    cur.executemany(
                        """
                        INSERT INTO records (
                            file_name, row_index, short_name, country, full_name,
                            email, wos_categories, research_areas,
                            raw_reprint, raw_email
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        records_buffer,
                    )
                    conn.commit()
                    records_buffer.clear()
            elif typ == "errors":
                batch = msg[1]
                if not batch:
                    continue
                errors_buffer.extend(batch)
                total_errors += len(batch)
                if len(errors_buffer) >= commit_batch_size:
                    cur.executemany(
                        """
                        INSERT INTO errors (
                            file_name, row_index, reason,
                            raw_reprint, raw_email, raw_row
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        errors_buffer,
                    )
                    conn.commit()
                    errors_buffer.clear()
            elif typ == "WORKER_DONE":
                done_workers += 1

        # 刷新剩余缓冲
        if records_buffer:
            cur.executemany(
                        """
                        INSERT INTO records (
                            file_name, row_index, short_name, country, full_name,
                            email, wos_categories, research_areas,
                            raw_reprint, raw_email
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                records_buffer,
            )
        if errors_buffer:
            cur.executemany(
                """
                INSERT INTO errors (
                    file_name, row_index, reason,
                    raw_reprint, raw_email, raw_row
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                errors_buffer,
            )
        if conn:
            conn.commit()
    except Exception as e:
        # 记录错误，但继续执行以确保发送 writer_done 消息
        writer_error = str(e)
        # 尝试提交已缓冲的数据
        if conn and cur is not None:
            try:
                if records_buffer:
                    cur.executemany(
                        """
                        INSERT INTO records (
                            file_name, row_index, short_name, country, full_name,
                            email, wos_categories, research_areas,
                            raw_reprint, raw_email
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        records_buffer,
                    )
                if errors_buffer:
                    cur.executemany(
                        """
                        INSERT INTO errors (
                            file_name, row_index, reason,
                            raw_reprint, raw_email, raw_row
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        errors_buffer,
                    )
                conn.commit()
            except Exception:
                pass  # 如果提交失败，至少尝试发送 writer_done 消息
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        # 确保 writer_done 消息必定发送，即使发生异常
        try:
            if writer_error:
                progress_queue.put(("writer_done", total_records, total_errors, writer_error))
            else:
                progress_queue.put(("writer_done", total_records, total_errors))
        except Exception:
            # 如果队列已满或关闭，尝试使用阻塞方式发送
            try:
                if writer_error:
                    progress_queue.put(("writer_done", total_records, total_errors, writer_error), block=True, timeout=5.0)
                else:
                    progress_queue.put(("writer_done", total_records, total_errors), block=True, timeout=5.0)
            except Exception:
                # 如果仍然失败，记录但继续执行
                pass


def _worker_main(
    task_queue: "mp.Queue[str | None]",
    result_queue: "mp.Queue[Tuple[str, Any]]",
    progress_queue: "mp.Queue[Tuple[Any, ...]]",
    config: Dict[str, str],
) -> None:
    """Worker 进程：从任务队列取文件路径，调用 process_file。"""
    while True:
        path = task_queue.get()
        if path is None:
            break
        process_file(path, config, result_queue, progress_queue)

    result_queue.put(("WORKER_DONE",))


def _scan_excel_files(input_dir: str) -> List[str]:
    """递归扫描目录下所有 .xls / .xlsx / .csv 文件。"""
    excel_files: List[str] = []
    for root, _dirs, files in os.walk(input_dir):
        for name in files:
            lower = name.lower()
            if lower.endswith(".xls") or lower.endswith(".xlsx") or lower.endswith(".csv"):
                excel_files.append(os.path.join(root, name))
    return excel_files


def run_etl(
    input_dir: str,
    db_path: str,
    config: Dict[str, str] | None = None,
    num_workers: int | None = None,
    progress_callback: Callable[[Dict[str, Any]], None] | None = None,
) -> None:
    """
    运行多进程 ETL：
        - 主进程：扫描文件、汇总进度、调用 progress_callback；
        - Worker 进程：逐文件解析，产生记录与错误；
        - Writer 进程：集中写 SQLite。
    """
    if not os.path.isdir(input_dir):
        raise ValueError(f"输入目录不存在：{input_dir}")

    files = _scan_excel_files(input_dir)
    total_files = len(files)

    if progress_callback:
        progress_callback({"type": "start", "total_files": total_files})

    if total_files == 0:
        return

    cfg = ensure_default_config(config or {})

    if num_workers is None or num_workers <= 0:
        num_workers = max(mp.cpu_count() - 1, 1)

    ctx = mp.get_context("spawn")
    task_queue: "mp.Queue[str | None]" = ctx.Queue()
    result_queue: "mp.Queue[Tuple[str, Any]]" = ctx.Queue()
    progress_queue: "mp.Queue[Tuple[Any, ...]]" = ctx.Queue()

    # 启动 Writer 进程
    writer = ctx.Process(
        target=_writer_process,
        args=(db_path, result_queue, progress_queue, num_workers),
        daemon=True,
    )
    writer.start()

    # 启动 Worker 进程
    workers: List[mp.Process] = []
    for _ in range(num_workers):
        p = ctx.Process(
            target=_worker_main,
            args=(task_queue, result_queue, progress_queue, cfg),
            daemon=True,
        )
        p.start()
        workers.append(p)

    # 投递任务
    for path in files:
        task_queue.put(path)
    # 发送结束标记
    for _ in range(num_workers):
        task_queue.put(None)

    completed_files = 0
    writer_done = False
    writer_error = None
    consecutive_empty_count = 0
    max_consecutive_empty = 10  # 最多等待 5 秒（0.5 * 10）

    while not writer_done:
        msg = None  # 避免在异常路径上未赋值就使用
        try:
            msg = progress_queue.get(timeout=0.5)
            consecutive_empty_count = 0  # 重置计数器
        except queue.Empty:
            consecutive_empty_count += 1
            # 检查 writer 进程是否还活着
            if not writer.is_alive():
                # Writer 进程异常退出，等待一小段时间确保所有消息都已处理
                import time
                time.sleep(0.1)
                # 尝试从队列中获取剩余消息
                try:
                    while True:
                        msg = progress_queue.get_nowait()
                        if msg and msg[0] == "writer_done":
                            writer_done = True
                            if len(msg) > 3:
                                writer_error = msg[3]
                            break
                except queue.Empty:
                    pass
                # 如果仍未收到 writer_done，说明 writer 异常退出
                if not writer_done:
                    writer_error = "Writer 进程异常退出，数据可能未完全写入"
                    writer_done = True
                break
            # 如果连续多次空队列且 writer 还活着，继续等待
            if consecutive_empty_count >= max_consecutive_empty:
                # 这种情况不应该发生，但为了安全起见，继续等待
                continue

        if not msg:
            continue

        typ = msg[0]
        if typ == "file_progress":
            _typ, file_name, succ, skip, err = msg
            completed_files += 1
            if progress_callback:
                progress_callback(
                    {
                        "type": "file_progress",
                        "file_name": file_name,
                        "completed_files": completed_files,
                        "total_files": total_files,
                        "success_rows": succ,
                        "skipped_rows": skip,
                        "error_rows": err,
                    }
                )
        elif typ == "writer_done":
            if len(msg) > 3:
                _typ, total_records, total_errors, writer_error = msg
            else:
                _typ, total_records, total_errors = msg
            writer_done = True
            if progress_callback:
                progress_callback(
                    {
                        "type": "writer_done",
                        "total_files": total_files,
                        "total_records": total_records,
                        "total_errors": total_errors,
                        "error": writer_error,
                    }
                )

    # 确保所有进程都已结束
    for p in workers:
        p.join(timeout=5.0)
        if p.is_alive():
            p.terminate()
            p.join()
    writer.join(timeout=5.0)
    if writer.is_alive():
        writer.terminate()
        writer.join()
    
    # 如果 writer 有错误，抛出异常
    if writer_error:
        raise RuntimeError(f"数据写入过程中发生错误：{writer_error}")

    if progress_callback:
        progress_callback(
            {
                "type": "finished",
                "total_files": total_files,
                "completed_files": completed_files,
            }
        )


