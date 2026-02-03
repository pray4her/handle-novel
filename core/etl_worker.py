from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

import pandas as pd

from .parsing import (
    AlignmentError,
    AuthorContact,
    map_short_to_full_names,
    normalize_semicolon_tags,
    parse_and_align_authors,
)

import re

def _contains_cjk(text: str) -> bool:
    if not text:
        return False
    for ch in str(text):
        if "\u4e00" <= ch <= "\u9fff":
            return True
    return False

def _get_potential_surnames(full_name: str) -> set[str]:
    if not full_name:
        return set()
    text = str(full_name).strip()
    if not text:
        return set()
    surnames = set()
    if "," in text:
        parts = text.split(",", 1)
        surnames.add(parts[0].strip().upper())
    else:
        tokens = text.split()
        if tokens:
            surnames.add(tokens[0].strip().upper())
            surnames.add(tokens[-1].strip().upper())
    return {s for s in surnames if s}

_COMMON_CHINESE_SURNAMES = {
    "LI","WANG","ZHANG","LIU","CHEN","YANG","ZHAO","HUANG","ZHOU","WU","XU","SUN","MA","ZHU","HU","GUO","HE","GAO","LIN","LUO","ZHENG","LIANG","XIE","SONG","TANG","HAN","FENG","PENG","CUI","JIANG","QIAN","QIN","YU","LU","SHI","YAO","CAO","DENG","YUAN","XIAO","XIONG","TAN","QIU","REN","YAN","DONG","CHENG","LAI","FAN","JIN","JIA","NI","SHEN","LIAO","LAN","QIAO","OU","HONG","CAI","PAN","TIAN","DU","DAI","XIA","ZHONG","YI","ZOU","SU","GU","HOU","WEI","TAO","FANG","BAI","HAO","KONG","SHAO","MENG","QUAN","WAN","LEI","BO","YIN","CHI","CHANG","MIAO","LUAN","YOU","GE","GONG","XING","RONG","WENG","JI","PING","BAO","MU","CHAN","WONG","LEE","CHEUNG","LAU","NG","YEUNG","YU","TSANG","CHUI","HO","KWOK","SUNG","POON","CHUNG","LEUNG","LAM","CHIANG","FONG","MOK","HUI","CHOI","SIN","TSUI","YIP","LUK","SIT","TAM","YIM","KAM","KWAN","TSE","AU","CHIU","CHOW","KO","LO","SIU","YUEN","YAU","FUNG","CHU","SHUM","YIU","TIN","TUNG","NGAN","LOK","HA","MO","HUNG","KUI","SHEK","LIM","CHUA","GOH","ONG","TEH","TEO","KOH","YEW","TEE","SOO","KHOO","YONG","FOO","CHEAH","TIAH","GAN","SIM","NEO","HENG","QUEK","AW","SEOW","LIAW","HOO","OON","TOH","DING","XUE","YE","CONG","YUE","CEN","XUN","PU","ZHA","SHUI","JIAO","ZHUANG","QU","YAN","MU","BU","SHA","NA","HE"
}

_LIKELY_KOREAN_SURNAMES = {"KIM","PARK","JEONG","MOON","SHIN","KANG","CHO","YUN","JANG"}

def _is_chinese_name(full_name: str, short_name: str) -> bool:
    combined = f"{full_name} {short_name}".strip()
    if _contains_cjk(combined):
        return True
    candidates = _get_potential_surnames(full_name or short_name)
    for sur in candidates:
        if sur in _COMMON_CHINESE_SURNAMES:
            return True
    return False

def _is_china_country(country: str) -> bool:
    if not country:
        return False
    text = str(country).upper()
    # 港澳台不视为中国大陆
    if any(re.search(rf"\b{re.escape(kw)}\b", text) for kw in ["HONG KONG", "MACAU", "MACAO", "TAIWAN"]):
        return False
    return any(re.search(rf"\b{re.escape(kw)}\b", text) for kw in ["CHINA", "PEOPLES R CHINA", "P R CHINA", "PR CHINA", "MAINLAND CHINA"])

def _is_non_chinese_asian_country(country: str) -> bool:
    if not country:
        return False
    text = str(country).upper()
    keywords = ["VIETNAM","VIET NAM","SOUTH KOREA","NORTH KOREA","REP KOREA","REPUBLIC OF KOREA","KOREA","JAPAN"]
    for kw in keywords:
        if re.search(rf"\b{re.escape(kw)}\b", text):
            return True
    return False

def classify_ethnic_chinese(full_name: str, short_name: str, country: str) -> str:
    if _is_china_country(country):
        return "国内华人"
    if _is_chinese_name(full_name, short_name):
        if _is_non_chinese_asian_country(country):
            return "外国人"
        return "海外华人"
    return "外国人"


# 默认列名配置，可被外部参数覆盖
DEFAULT_COLUMNS: Dict[str, str] = {
    "author_full_names_col": "Author Full Names",
    "reprint_col": "Reprint Addresses",
    "email_col": "Email Addresses",
    "addresses_col": "Addresses",
    "wos_categories_col": "WoS Categories",
    "research_areas_col": "Research Areas",
}


def ensure_default_config(config: Dict[str, str] | None) -> Dict[str, str]:
    """将用户配置与默认列名合并。"""
    merged = DEFAULT_COLUMNS.copy()
    if config:
        merged.update(config)
    return merged


def _flush_batches(
    result_queue,
    records_batch: List[Tuple[Any, ...]],
    errors_batch: List[Tuple[Any, ...]],
) -> None:
    """将当前批次的记录/错误推送到结果队列。"""
    if records_batch:
        result_queue.put(("records", records_batch.copy()))
        records_batch.clear()
    if errors_batch:
        result_queue.put(("errors", errors_batch.copy()))
        errors_batch.clear()


def _serialize_row(row: pd.Series | None) -> str:
    if row is None:
        return ""
    try:
        return json.dumps(row.to_dict(), ensure_ascii=False)
    except Exception:
        return ""


def process_file(
    path: str,
    config: Dict[str, str],
    result_queue,
    progress_queue,
    record_batch_size: int = 500,
    error_batch_size: int = 200,
) -> None:
    """
    处理单个 Excel/CSV 文件：
        - 读取指定列（支持 .xls, .xlsx, .csv 格式）；
        - 按行解析通讯作者与邮箱；
        - 写入 records/errors 批次到 result_queue；
        - 将文件级统计信息写入 progress_queue。
    """
    cfg = ensure_default_config(config)
    file_name = os.path.basename(path)

    records_batch: List[Tuple[Any, ...]] = []
    errors_batch: List[Tuple[Any, ...]] = []

    success_rows = 0
    skipped_rows = 0
    error_rows = 0

    try:
        # 根据扩展名选择合适的读取方式：
        #   - .csv 使用 read_csv
        #   - .xls 使用 xlrd
        #   - 其他（如 .xlsx）交给 pandas 自动选择（通常为 openpyxl）
        ext = os.path.splitext(path)[1].lower()
        if ext == ".csv":
            # 读取 CSV 文件，尝试不同的编码格式
            encodings = ["utf-8", "gbk", "gb2312", "latin-1"]
            df = None
            for encoding in encodings:
                try:
                    df = pd.read_csv(path, dtype=str, encoding=encoding)
                    break
                except UnicodeDecodeError:
                    continue
            if df is None:
                # 如果所有编码都失败，使用默认编码并忽略错误
                df = pd.read_csv(path, dtype=str, encoding="utf-8", errors="ignore")
        elif ext == ".xls":
            df = pd.read_excel(path, dtype=str, engine="xlrd")
        else:
            df = pd.read_excel(path, dtype=str)
    except Exception as e:  # 文件级错误
        errors_batch.append(
            (
                file_name,
                -1,
                f"FILE_READ_ERROR: {e}",
                "",
                "",
                "",
            )
        )
        error_rows += 1
        _flush_batches(result_queue, records_batch, errors_batch)
        progress_queue.put(
            ("file_progress", file_name, success_rows, skipped_rows, error_rows)
        )
        return

    # 仅强制要求核心列存在：Reprint 与 Email。其余列可选（缺失时使用空字符串）。
    required_cols = [cfg["reprint_col"], cfg["email_col"]]
    missing_cols = [col for col in required_cols if col not in df.columns]  # type: ignore[arg-type]
    if missing_cols:
        errors_batch.append(
            (
                file_name,
                -1,
                f"MISSING_COLUMNS: {', '.join(missing_cols)}",
                "",
                "",
                "",
            )
        )
        error_rows += len(df)
        _flush_batches(result_queue, records_batch, errors_batch)
        progress_queue.put(
            ("file_progress", file_name, success_rows, skipped_rows, error_rows)
        )
        return

    df = df.fillna("")

    for idx, row in df.iterrows():
        row_payload = _serialize_row(row)
        try:
            reprint_str = str(row[cfg["reprint_col"]]).strip()
            email_str = str(row[cfg["email_col"]]).strip()
            # 尝试读取 Addresses 列，如果不存在或为空则为 None
            addresses_str = None
            if cfg["addresses_col"] in row:
                addr_val = str(row[cfg["addresses_col"]]).strip()
                if addr_val and addr_val.lower() not in ("", "nan", "none"):
                    addresses_str = addr_val
        except Exception as e:
            errors_batch.append(
                (
                    file_name,
                    int(idx),
                    f"ROW_ACCESS_ERROR: {e!r}",
                    "",
                    "",
                    row_payload,
                )
            )
            error_rows += 1
            if len(errors_batch) >= error_batch_size:
                _flush_batches(result_queue, records_batch, errors_batch)
            continue

        if not reprint_str or not email_str:
            errors_batch.append(
                (
                    file_name,
                    int(idx),
                    "SKIP: empty reprint or email",
                    reprint_str,
                    email_str,
                    row_payload,
                )
            )
            skipped_rows += 1
            if len(errors_batch) >= error_batch_size:
                _flush_batches(result_queue, records_batch, errors_batch)
            continue

        # 过滤掉邮箱前缀为纯数字的项，并记录错误
        emails_raw_list = [em.strip() for em in str(email_str).split(";") if em.strip()]
        invalid_emails = []
        valid_emails_list = []
        for em in emails_raw_list:
            prefix = em.split("@", 1)[0].strip()
            if prefix.isdigit():
                invalid_emails.append(em)
            else:
                valid_emails_list.append(em)

        if invalid_emails:
            for bad in invalid_emails:
                errors_batch.append(
                    (
                        file_name,
                        int(idx),
                        "SKIP: email_prefix_numeric",
                        reprint_str,
                        bad,
                        row_payload,
                    )
                )
            if len(errors_batch) >= error_batch_size:
                _flush_batches(result_queue, records_batch, errors_batch)

        if not valid_emails_list:
            skipped_rows += 1
            continue

        try:
            pairs = parse_and_align_authors(reprint_str, "; ".join(valid_emails_list), addresses_str)
        except AlignmentError as e:
            errors_batch.append(
                (
                    file_name,
                    int(idx),
                    f"SKIP: {e}",
                    reprint_str,
                    email_str,
                    row_payload,
                )
            )
            skipped_rows += 1
        except Exception as e:
            errors_batch.append(
                (
                    file_name,
                    int(idx),
                    f"EXCEPTION: {e!r}",
                    reprint_str,
                    email_str,
                    row_payload,
                )
            )
            error_rows += 1
        else:
            author_full_names_str = ""
            if cfg["author_full_names_col"] in row:
                author_full_names_str = str(row[cfg["author_full_names_col"]])
            # 只对需要查找 full_name 的 contact 调用 map_short_to_full_names
            needs_mapping = [contact for contact in pairs if not contact.full_name]
            full_map = {}
            if needs_mapping:
                full_map = map_short_to_full_names(
                    [contact.short_name for contact in needs_mapping],
                    author_full_names_str,
                )
            
            wos_cat = ""
            research_areas = ""
            if cfg["wos_categories_col"] in row:
                wos_cat = normalize_semicolon_tags(row[cfg["wos_categories_col"]])
            if cfg["research_areas_col"] in row:
                research_areas = normalize_semicolon_tags(row[cfg["research_areas_col"]])

            for contact in pairs:
                short_name = contact.short_name
                email = contact.email
                country = contact.country
                # 优先使用 contact 中已有的 full_name（来自 Addresses 列）
                # 如果没有，则从 full_map 中查找（来自 Author Full Names 列）
                full_name = contact.full_name or full_map.get(short_name) or ""
                similarity = getattr(contact, "similarity", 0.0) or 0.0
                ethnic = classify_ethnic_chinese(full_name, short_name, country)
                records_batch.append(
                    (
                        file_name,
                        int(idx),
                        short_name,
                        country,
                        ethnic,
                        full_name,
                        email,
                        wos_cat,
                        research_areas,
                        reprint_str,
                        email_str,
                        float(similarity),
                    )
                )
            success_rows += 1

        if len(records_batch) >= record_batch_size or len(errors_batch) >= error_batch_size:
            _flush_batches(result_queue, records_batch, errors_batch)

    _flush_batches(result_queue, records_batch, errors_batch)

    progress_queue.put(
        ("file_progress", file_name, success_rows, skipped_rows, error_rows)
    )
