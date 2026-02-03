from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import List, Optional, Sequence, Tuple

import pandas as pd
from .concurrency import _ensure_db_schema


# 常见中文姓氏（包含拼音及常见海外拼写），用于判断“是否为中国人名字”
# 注意：该列表移除了部分极易与英文单词混淆的拼写（如 MAN, LAW, KING, YOUNG, LONG, DAY 等），
# 以降低“海外华人”判定时的假阳性率。同时保留了 LEE，但需注意其在韩国与英语圈也极常见。
_COMMON_CHINESE_SURNAMES = {
    # --- 拼音 (Pinyin) ---
    "LI", "WANG", "ZHANG", "LIU", "CHEN", "YANG", "ZHAO", "HUANG", "ZHOU", "WU",
    "XU", "SUN", "MA", "ZHU", "HU", "GUO", "HE", "GAO", "LIN", "LUO",
    "ZHENG", "LIANG", "XIE", "SONG", "TANG", "HAN", "FENG", "PENG", "CUI", "JIANG",
    "QIAN", "QIN", "YU", "LU", "SHI", "YAO", "CAO", "DENG", "YUAN", "XIAO",
    "XIONG", "TAN", "QIU", "REN", "YAN", "DONG", "CHENG", "LAI", "FAN", "JIN",
    "JIA", "NI", "SHEN", "LIAO", "LAN", "QIAO", "OU", "HONG", "CAI", "PAN",
    "TIAN", "DU", "DAI", "XIA", "ZHONG", "YI", "ZOU", "SU", "GU", "HOU",
    "WEI", "TAO", "FANG", "BAI", "HAO", "KONG", "SHAO", "MENG", "QUAN",
    "WAN", "LEI", "BO", "YIN", "CHI", "CHANG", "MIAO", "LUAN", "YOU", "GE",
    "GONG", "XING", "RONG", "WENG", "JI", "PING", "BAO", "MU",
    
    # --- 粤语/闽南语/客家语/海外常见拼写 (Cantonese / Hokkien / Teochew / Hakka) ---
    "CHAN", "WONG", "LEE", "CHEUNG", "LAU", "NG", "YEUNG", "YU", "TSANG",
    "CHUI", "HO", "KWOK", "SUNG", "POON", "CHUNG", "LEUNG", "LAM", "CHIANG", "FONG",
    "MOK", "HUI", "CHOI", "SIN", "TSUI", "YIP", "LUK", "SIT", "TAM", "YIM",
    "KAM", "KWAN", "TSE", "AU", "CHIU", "CHOW",
    "KO", "LO", "SIU", "YUEN", "YAU", "FUNG", "CHU", "SHUM", "YIU",
    "TIN", "TUNG", "NGAN", "LOK", "HA", "MO", "HUNG", "KUI", "SHEK",
    
    "LIM", "CHUA", "GOH", "ONG", "TEH", "TEO", "KOH", "YEW", "TEE",
    "SOO", "KHOO", "YONG", "FOO", "CHEAH", "TIAH", "GAN", "SIM",
    "NEO", "HENG", "QUEK", "AW", "SEOW", "LIAW", "HOO", "OON", "TOH",
    
    # --- 补充常见单字拼音 ---
    "DING", "XUE", "YE", "CONG", "YUE", "CEN", "XUN", "PU", "ZHA",
    "SHUI", "JIAO", "ZHUANG", "QU", "YAN", "MU", "BU", "SHA", "NA", "HE",
}

# 明确的韩国姓氏（排除常见重叠），用于辅助过滤
_LIKELY_KOREAN_SURNAMES = {
    "KIM", "PARK", "JEONG", "MOON", "SHIN", "KANG", "CHO", "YUN", "JANG", "LIM"
    # 注意：LIM 在福建/潮州人中常用，但在韩国也作林(Im/Lim)，这里需权衡。
    # 鉴于 LIM 在东南亚华人中极多，暂不将其仅归为韩国。
    # 但 KIM 和 PARK 是极强的韩国信号。
}
_LIKELY_KOREAN_SURNAMES.remove("LIM")


def _contains_cjk(text: str) -> bool:
    """是否包含中日韩汉字，用于快速判断是否可能是中文名。"""
    if not text:
        return False
    for ch in str(text):
        if "\u4e00" <= ch <= "\u9fff":
            return True
    return False


def _get_potential_surnames(full_name: str) -> set[str]:
    """
    从作者全名中提取可能的姓氏（英文拼写）。
    返回一个集合，包含所有可能的姓氏候选。
    """
    if not full_name:
        return set()
    text = str(full_name).strip()
    if not text:
        return set()

    surnames = set()
    
    # 策略 1: 逗号分隔 (WoS 标准格式: "Surname, Given Name")
    if "," in text:
        parts = text.split(",", 1)
        surnames.add(parts[0].strip().upper())
    else:
        # 策略 2: 空格分隔 (可能是 "Given Surname" 或 "Surname Given")
        tokens = text.split()
        if tokens:
            # 假设姓氏在首或尾
            surnames.add(tokens[0].strip().upper())
            surnames.add(tokens[-1].strip().upper())
            
    return {s for s in surnames if s}


def _is_chinese_name(full_name: str, short_name: str) -> bool:
    """
    判断姓名是否“看起来像中国人名字”：
        1. 若包含中文汉字，则视为中国人名字；
        2. 若为纯英文名，则检查提取的姓氏是否在常见中文姓氏表中。
    """
    combined = f"{full_name} {short_name}".strip()
    if _contains_cjk(combined):
        return True

    # 提取所有可能的姓氏候选
    candidates = _get_potential_surnames(full_name or short_name)
    
    # 只要有一个候选姓氏是明确的中文姓氏，即判为 True
    # 这种策略偏向于 Recall（召回率），可能会有一定的 False Positive（如 David Lee 被判为华人）
    for sur in candidates:
        if sur in _COMMON_CHINESE_SURNAMES:
            return True
            
    return False


def _is_china_country(country: str) -> bool:
    if not country:
        return False
    text = str(country).upper()

    # 若包含港澳台关键词，则不视为中国大陆
    if ("HONG KONG" in text) or ("MACAU" in text) or ("MACAO" in text) or ("TAIWAN" in text):
        return False

    # 明确的中国大陆关键词
    if ("PEOPLES R CHINA" in text) or ("P R CHINA" in text) or ("PEOPLE'S R CHINA" in text) or re.search(r"\bMAINLAND CHINA\b", text):
        return True

    # 单独的 "CHINA"（排除诸如 INDOCHINA 等情况由单词边界控制）
    if re.search(r"\bCHINA\b", text):
        return True

    return False


def _is_non_chinese_asian_country(country: str) -> bool:
    """
    判断国家是否为非中文的亚洲国家（如越南、韩国、日本）。
    这些国家的姓氏拼写常与中文拼音或方言拼写重叠（如 Le, Ha, Lee, Lim, Ma），
    若不排除，极易被误判为“海外华人”。
    """
    if not country:
        return False
    text = str(country).upper()
    
    # 关键词列表
    keywords = [
        "VIETNAM", "VIET NAM",
        "SOUTH KOREA", "NORTH KOREA", "REP KOREA", "REPUBLIC OF KOREA", "KOREA",
        "JAPAN",
    ]
    
    for kw in keywords:
        # 使用单词边界匹配，防止误判
        if re.search(rf"\b{re.escape(kw)}\b", text):
            return True
    return False


def classify_ethnic_chinese(full_name: str, short_name: str, country: str) -> str:
    """
    综合“姓名 + 国籍”判断华人类型：
        - 若看起来像中国人名字，且国家为中国 -> "国内华人"
        - 若看起来像中国人名字，且国家为非中国 -> "海外华人"
        - 否则 -> "外国人"
    """
    is_cn_name = _is_chinese_name(full_name, short_name)
    is_cn_country = _is_china_country(country)

    # 规则修正：
    #   - 只要国家字段判定为中国，即视为“国内华人”（即便姓名未能识别为中文名，避免误判为外国人）；
    #   - 否则，再根据姓名是否像中国人名字，区分“海外华人”与“外国人”。
    if is_cn_country:
        return "国内华人"
    
    if is_cn_name:
        # 进一步修正：如果国家是越南、韩国、日本等，且名字看起来像中文（因为历史文化原因重叠），
        # 除非有极强证据（目前暂无），否则优先判定为当地人（即外国人），而非海外华人。
        if _is_non_chinese_asian_country(country):
            return "外国人"
            
        return "海外华人"
        
    return "外国人"


def _build_filter_sql(
    wos_keywords: Optional[Sequence[str]],
    research_keywords: Optional[Sequence[str]],
    file_name_keywords: Optional[Sequence[str]] = None,
    country_keywords: Optional[Sequence[str]] = None,
) -> Tuple[str, List[str]]:
    """
    根据关键词构建 WHERE 子句与参数。

    - wos_keywords: 作用于 wos_categories 列；
    - research_keywords: 作用于 research_areas 列；
    - file_name_keywords: 作用于 file_name 列；
    - country_keywords: 作用于 country 列。
    """
    conditions = []
    params: List[str] = []

    if file_name_keywords:
        sub = " OR ".join("file_name LIKE ?" for _ in file_name_keywords)
        conditions.append(f"({sub})")
        params.extend([f"%{kw}%" for kw in file_name_keywords])

    if country_keywords:
        sub = " OR ".join("country LIKE ?" for _ in country_keywords)
        conditions.append(f"({sub})")
        params.extend([f"%{kw}%" for kw in country_keywords])

    if wos_keywords:
        sub = " OR ".join("wos_categories LIKE ?" for _ in wos_keywords)
        conditions.append(f"({sub})")
        params.extend([f"%{kw}%" for kw in wos_keywords])

    if research_keywords:
        sub = " OR ".join("research_areas LIKE ?" for _ in research_keywords)
        conditions.append(f"({sub})")
        params.extend([f"%{kw}%" for kw in research_keywords])

    where = ""
    if conditions:
        where = " WHERE " + " AND ".join(conditions)

    return where, params


def export_to_excel_by_tags(
    db_path: str,
    output_dir: str,
    wos_keywords: Optional[Sequence[str]] = None,
    research_keywords: Optional[Sequence[str]] = None,
    file_name_keywords: Optional[Sequence[str]] = None,
    country_keywords: Optional[Sequence[str]] = None,
    ethnic_filters: Optional[Sequence[str]] = None,
    chunk_size: int = 10_000,
    include_file_name: bool = True,
    include_country: bool = True,
    include_ethnic_chinese: bool = True,
    min_similarity: Optional[float] = None,
) -> List[str]:
    """
    按 WoS / Research Areas 关键词从 SQLite 导出记录到 Excel，并按块切分。

    返回导出的文件路径列表。
    """
    os.makedirs(output_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        # 确保数据库模式包含新列（向后兼容旧数据库）
        _ensure_db_schema(conn)
        # 针对大数据量优化：在导出前确保索引存在
        # 注意：对于大型数据库，首次创建索引可能需要几分钟时间，但这是为了后续查询速度必须的代价。
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_wos ON records(wos_categories)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_research ON records(research_areas)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_filename ON records(file_name)")
        conn.commit()

        where, params = _build_filter_sql(
            wos_keywords=wos_keywords,
            research_keywords=research_keywords,
            file_name_keywords=file_name_keywords,
            country_keywords=country_keywords,
        )
        
        # 优化：使用 keyset pagination (WHERE id > last_id) 代替 OFFSET
        # 这在处理大数据量时性能更好（避免扫描并丢弃前面的行）
        base_where = " WHERE " if not where else where + " AND "
        
        select_sql = f"""
            SELECT
                id, file_name, row_index, short_name, country, ethnic_chinese, full_name,
                email, wos_categories, research_areas, email_validity, similarity
            FROM records
            {base_where} id > ?
            ORDER BY id ASC
            LIMIT ?
        """
        
        # 合并参数：过滤参数 + id_lower_bound + chunk_size
        # 注意：params 已经在 _build_filter_sql 中生成，这里我们需要在循环中动态添加 id 和 limit
        
        exported_files: List[str] = []
        last_id = 0
        chunk_index = 1

        # 构造导出文件前缀
        label_parts = []
        if wos_keywords:
            label_parts.append("WOS_" + "_".join(wos_keywords))
        if research_keywords:
            label_parts.append("RA_" + "_".join(research_keywords))
        if file_name_keywords:
            label_parts.append("FILE_" + "_".join(file_name_keywords))
        if country_keywords:
            label_parts.append("CTRY_" + "_".join(country_keywords))
        if ethnic_filters:
            label_parts.append("ETH_" + "_".join(ethnic_filters))
        if not label_parts:
            label_base = "all"
        else:
            label_base = "_".join(label_parts)
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label_base)
        run_ts = time.strftime("%Y%m%d_%H%M%S")

        columns = [
            "id",  # 为了调试和追踪，包含ID
            "file_name",
            "row_index",
            "short_name",
            "country",
            "ethnic_chinese",
            "full_name",
            "email",
            "wos_categories",
            "research_areas",
            "email_validity",
            "similarity",
        ]
        
        # Excel 默认导出列（不含 id），再根据参数决定是否导出原始文件名/国家/华人华裔列
        base_excel_columns = columns[1:]
        excel_columns: List[str] = []
        for col in base_excel_columns:
            if col == "file_name" and not include_file_name:
                continue
            if col == "country" and not include_country:
                continue
            excel_columns.append(col)

        # 当按华人华裔过滤时，直接从数据库一次读取 chunk_size 条后再过滤，可能导致导出文件远小于期望的 chunk_size。
        # 为了保证每个导出文件尽量包含 chunk_size 条满足过滤条件的记录，需要在内存中累计匹配行直到达到 chunk_size 或数据库耗尽。
        fetch_batch_size = max(1000, min(chunk_size, 100_000))
        while True:
            accumulated_rows: List[Tuple] = []
            max_id_seen_in_round = last_id

            # 若不按 ethnic_filters 实际过滤（包括不计算 ethnic 列的情况），保持原有行为：一次读取 chunk_size 行
            if not include_ethnic_chinese or not ethnic_filters:
                query_params = list(params) + [last_id, chunk_size]
                cur = conn.execute(select_sql, query_params)
                rows = cur.fetchall()
                if not rows:
                    break
                batch_max_id = max(int(r[0]) for r in rows)
                max_id_seen_in_round = max(max_id_seen_in_round, batch_max_id)
                df = pd.DataFrame(rows, columns=columns)
                if "row_index" in df.columns:
                    df["row_index"] = df["row_index"].astype(int) + 2
                # ethnic_chinese 列来自数据库
                if min_similarity is not None and "similarity" in df.columns:
                    df["similarity"] = pd.to_numeric(df["similarity"], errors="coerce").fillna(0.0)
                    df = df[df["similarity"] >= float(min_similarity)]
                accumulated_rows = [tuple(x) for x in df.to_records(index=False)]
            else:
                # 需要按 ethnic_filters 过滤：循环拉取小批量，累积满足过滤的行
                while len(accumulated_rows) < chunk_size:
                    # Fix: use max_id_seen_in_round instead of last_id to advance cursor in inner loop
                    query_params = list(params) + [max_id_seen_in_round, fetch_batch_size]
                    cur = conn.execute(select_sql, query_params)
                    rows = cur.fetchall()
                    if not rows:
                        break

                    batch_max_id = max(int(r[0]) for r in rows)
                    max_id_seen_in_round = max(max_id_seen_in_round, batch_max_id)

                    df = pd.DataFrame(rows, columns=columns)
                    if "row_index" in df.columns:
                        df["row_index"] = df["row_index"].astype(int) + 2

                    # ethnic_chinese 列来自数据库
                    matched = df[df["ethnic_chinese"].isin(list(ethnic_filters))]
                    if min_similarity is not None and "similarity" in matched.columns:
                        matched["similarity"] = pd.to_numeric(matched["similarity"], errors="coerce").fillna(0.0)
                        matched = matched[matched["similarity"] >= float(min_similarity)]
                    if not matched.empty:
                        accumulated_rows.extend([tuple(x) for x in matched.to_records(index=False)])

                    # 暂不推进全局游标；在写出后以“已导出最大ID”为准推进，避免丢行

                if not accumulated_rows:
                    # 数据库已读尽且本轮没有匹配行，结束导出
                    break

            # 根据是否包含 ethnic_chinese 列来确定正确的列名
            out_columns_for_df = list(columns)
            
            out_df = pd.DataFrame(accumulated_rows, columns=out_columns_for_df)
            if out_df.empty:
                break

            # 若累积超过 chunk_size，则截断
            if len(out_df) > chunk_size:
                out_df = out_df.iloc[:chunk_size]

            # 推进全局游标到“本次已导出的最大 ID”，以避免丢失超过 chunk_size 的匹配行
            if "id" in out_df.columns and not out_df.empty:
                try:
                    last_id = int(out_df["id"].max())
                except Exception:
                    last_id = max_id_seen_in_round
            else:
                last_id = max_id_seen_in_round

            out_columns = list(excel_columns)
            if include_ethnic_chinese:
                out_columns.append("ethnic_chinese")

            sim_tag = ""
            if min_similarity is not None and float(min_similarity) > 0.0:
                sim_tag = f"_minSim{int(round(float(min_similarity) * 100))}"
            out_name = f"{safe_label}{sim_tag}_{run_ts}_{chunk_index:04d}.xlsx"
            out_path = os.path.join(output_dir, out_name)

            out_df[out_columns].to_excel(out_path, index=False)
            exported_files.append(out_path)

            chunk_index += 1

        return exported_files
    finally:
        conn.close()

def reclassify_ethnic_chinese(db_path: str, batch_size: int = 10000) -> int:
    conn = sqlite3.connect(db_path)
    try:
        _ensure_db_schema(conn)
        total = 0
        last_id = 0
        while True:
            cur = conn.execute(
                """
                SELECT id, full_name, short_name, country
                FROM records
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (last_id, int(batch_size)),
            )
            rows = cur.fetchall()
            if not rows:
                break
            updates = []
            for row in rows:
                rid = int(row[0])
                full_name = str(row[1] or "")
                short_name = str(row[2] or "")
                country = str(row[3] or "")
                cls = classify_ethnic_chinese(full_name, short_name, country)
                updates.append((cls, rid))
            conn.executemany(
                "UPDATE records SET ethnic_chinese = ? WHERE id = ?",
                updates,
            )
            conn.commit()
            last_id = max(int(r[0]) for r in rows)
            total += len(rows)
        return total
    finally:
        conn.close()




def remove_duplicates(db_path: str) -> int:
    """
    对数据库中的记录进行去重，基于 (short_name, email) 组合。
    保留 id 最小的一条，删除其余重复项。
    返回删除的记录数。
    """
    conn = sqlite3.connect(db_path)
    try:
        # 1. 确保有索引以加速 GROUP BY 查询
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_dedup ON records(short_name, email)")
        conn.commit()

        # 2. 统计当前总数
        cur = conn.execute("SELECT COUNT(*) FROM records")
        initial_count = cur.fetchone()[0]

        # 3. 执行去重
        # 策略：删除 id NOT IN (每组 (short_name, email) 的最小 id)
        conn.execute(
            """
            DELETE FROM records
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM records
                GROUP BY short_name, email
            )
            """
        )
        conn.commit()

        # 4. 统计剩余总数
        cur = conn.execute("SELECT COUNT(*) FROM records")
        final_count = cur.fetchone()[0]

        removed_count = initial_count - final_count
        
        # 5. 若有删除，执行 VACUUM 释放空间
        if removed_count > 0:
             conn.execute("VACUUM")

        return removed_count
    finally:
        conn.close()


def _generate_new_db_path(original: str, suffix: str) -> str:
    base_dir = os.path.dirname(os.path.abspath(original))
    stem = os.path.splitext(os.path.basename(original))[0]
    ts = time.strftime("%Y%m%d_%H%M%S")
    candidate = os.path.join(base_dir, f"{stem}_{suffix}_{ts}.db")
    if not os.path.exists(candidate):
        return candidate
    i = 1
    while True:
        alt = os.path.join(base_dir, f"{stem}_{suffix}_{ts}_{i}.db")
        if not os.path.exists(alt):
            return alt
        i += 1


def remove_duplicates_to_new_db(db_path: str, output_db_path: Optional[str] = None) -> Tuple[str, int, int]:
    """
    非破坏性“普通去重”：基于 (short_name, email) 组合保留 id 最小的一条，
    将结果写入新的 SQLite 文件，不修改原库。

    返回 (新库路径, 保留条数, 删除条数)。
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite 文件不存在：{db_path}")

    new_db = output_db_path or _generate_new_db_path(db_path, "dedup")

    conn = sqlite3.connect(new_db)
    try:
        _ensure_db_schema(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_dedup ON records(short_name, email)")
        conn.commit()

        conn.execute("ATTACH DATABASE ? AS src_db", (os.path.abspath(db_path),))

        conn.execute(
            """
            INSERT INTO records (
                file_name, row_index, short_name, country, ethnic_chinese, full_name,
                email, wos_categories, research_areas,
                raw_reprint, raw_email, email_validity, email_validation_attempts, similarity
            )
            SELECT
                r.file_name, r.row_index, r.short_name, r.country, r.ethnic_chinese, r.full_name,
                r.email, r.wos_categories, r.research_areas,
                r.raw_reprint, r.raw_email, r.email_validity, r.email_validation_attempts, COALESCE(r.similarity, 0.0)
            FROM src_db.records AS r
            JOIN (
                SELECT short_name, email, MIN(id) AS min_id
                FROM src_db.records
                GROUP BY short_name, email
            ) AS t
            ON r.short_name = t.short_name AND r.email = t.email AND r.id = t.min_id
            """
        )

        # 复制 errors 表（不参与去重）
        conn.execute(
            """
            INSERT INTO errors (
                file_name, row_index, reason,
                raw_reprint, raw_email, raw_row
            )
            SELECT file_name, row_index, reason, raw_reprint, raw_email, raw_row
            FROM src_db.errors
            """
        )
        conn.commit()

        cur = conn.execute("SELECT COUNT(*) FROM records")
        kept = int(cur.fetchone()[0])
        cur2 = conn.execute("SELECT COUNT(*) FROM src_db.records")
        original = int(cur2.fetchone()[0])
        removed = original - kept
        # 清理附件
        conn.execute("DETACH DATABASE src_db")
        return new_db, kept, removed
    finally:
        conn.close()


def deduplicate_by_similarity(db_path: str, output_db_path: Optional[str] = None) -> Tuple[str, int, int]:
    """
    精细化去重：按邮箱分组，仅保留“相似度最高”的一条（相同相似度时取 id 最小），
    写入新的 SQLite 文件，不修改原库。

    返回 (新库路径, 保留条数, 删除条数)。
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite 文件不存在：{db_path}")

    new_db = output_db_path or _generate_new_db_path(db_path, "email_dedup")

    conn = sqlite3.connect(new_db)
    try:
        _ensure_db_schema(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_email ON records(email)")
        conn.commit()

        conn.execute("ATTACH DATABASE ? AS src_db", (os.path.abspath(db_path),))

        # 使用联接与排除法选择“相似度最高且最小 id”的记录
        conn.execute(
            """
            INSERT INTO records (
                file_name, row_index, short_name, country, ethnic_chinese, full_name,
                email, wos_categories, research_areas,
                raw_reprint, raw_email, email_validity, email_validation_attempts, similarity
            )
            SELECT
                r.file_name, r.row_index, r.short_name, r.country, r.ethnic_chinese, r.full_name,
                r.email, r.wos_categories, r.research_areas,
                r.raw_reprint, r.raw_email, r.email_validity, r.email_validation_attempts, COALESCE(r.similarity, 0.0)
            FROM src_db.records AS r
            JOIN (
                SELECT email, MAX(COALESCE(similarity, 0.0)) AS max_sim
                FROM src_db.records
                GROUP BY email
            ) AS m
              ON r.email = m.email AND COALESCE(r.similarity, 0.0) = m.max_sim
            LEFT JOIN src_db.records AS r2
              ON r2.email = r.email
             AND COALESCE(r2.similarity, 0.0) = COALESCE(r.similarity, 0.0)
             AND r2.id < r.id
            WHERE r2.id IS NULL
            """
        )

        conn.execute(
            """
            INSERT INTO errors (
                file_name, row_index, reason,
                raw_reprint, raw_email, raw_row
            )
            SELECT file_name, row_index, reason, raw_reprint, raw_email, raw_row
            FROM src_db.errors
            """
        )
        conn.commit()

        cur = conn.execute("SELECT COUNT(*) FROM records")
        kept = int(cur.fetchone()[0])
        cur2 = conn.execute("SELECT COUNT(*) FROM src_db.records")
        original = int(cur2.fetchone()[0])
        removed = original - kept
        conn.execute("DETACH DATABASE src_db")
        return new_db, kept, removed
    finally:
        conn.close()


def export_errors_to_excel(
    db_path: str,
    output_dir: str,
    chunk_size: int = 10_000,
) -> List[str]:


    """
    将 errors 表导出为 Excel 文件，并按块切分。

    导出格式：
        - 前三列固定为 file_name、row_index、reason；
        - 其余列为原始数据行（raw_row JSON）中的所有字段，保持原列顺序。

    返回导出的文件路径列表。
    """
    os.makedirs(output_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        exported_files: List[str] = []
        chunk_index = 1
        last_id = 0  # keyset 分页游标

        while True:
            # 使用 keyset 分页（WHERE id > last_id ORDER BY id LIMIT ?）
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    id,
                    file_name,
                    row_index,
                    reason,
                    raw_row
                FROM errors
                WHERE id > ?
                ORDER BY id
                LIMIT ?
                """,
                (last_id, chunk_size),
            )
            rows = cur.fetchall()
            if not rows:
                break

            # 处理每一行的数据
            records: List[dict] = []
            max_id_in_batch = last_id
            for row in rows:
                row_id = int(row[0])
                if row_id > max_id_in_batch:
                    max_id_in_batch = row_id

                # 将存储的 0 基索引转换为 Excel 中的实际行号（包含表头，因此 +2）
                raw_row_index = row[2]
                try:
                    row_index_int = int(raw_row_index) if raw_row_index is not None else -1
                except (TypeError, ValueError):
                    row_index_int = -1
                if row_index_int >= 0:
                    display_row_index = row_index_int + 2
                else:
                    # 文件级错误等使用 -1，保持为 -1 以表示“无具体行号”
                    display_row_index = row_index_int

                record = {
                    "file_name": row[1],
                    "row_index": display_row_index,
                    "reason": row[3],
                }
                payload = row[4] if len(row) > 4 else None
                raw_row_dict = None
                if isinstance(payload, str) and payload:
                    try:
                        raw_row_dict = json.loads(payload)
                    except Exception:
                        raw_row_dict = None
                if isinstance(raw_row_dict, dict):
                    record.update(raw_row_dict)

                records.append(record)

            # 更新分页游标
            last_id = max_id_in_batch

            if records:
                export_df = pd.DataFrame(records)
            else:
                export_df = pd.DataFrame(columns=["file_name", "row_index", "reason"])

            # 导出到文件
            out_name = f"errors_{chunk_index:04d}.xlsx"
            out_path = os.path.join(output_dir, out_name)
            export_df.to_excel(out_path, index=False)
            exported_files.append(out_path)

            chunk_index += 1

        return exported_files
    finally:
        conn.close()
def create_low_similarity_db(
    db_path: str,
    min_similarity: float,
    wos_keywords: Optional[Sequence[str]] = None,
    research_keywords: Optional[Sequence[str]] = None,
    file_name_keywords: Optional[Sequence[str]] = None,
    country_keywords: Optional[Sequence[str]] = None,
    ethnic_filters: Optional[Sequence[str]] = None,
    output_db_path: Optional[str] = None,
) -> Tuple[str, int]:
    """
    基于当前过滤条件，生成“低相似度”记录的新库：
    - 选择 COALESCE(similarity, 0.0) < min_similarity 的记录；
    - 不修改原库，写入新的 SQLite 文件；
    - 复制 errors 表，便于追踪。

    返回 (新库路径, 记录条数)。
    注：ethnic 过滤在导出阶段计算，数据库不存储该列，因此此处仅按 WoS/Research/文件名/国家过滤。
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite 文件不存在：{db_path}")

    # 生成新库文件名：<原库名>_lowSim_minXX_YYYYmmdd_HHMMSS.db
    base_dir = os.path.dirname(os.path.abspath(db_path))
    stem = os.path.splitext(os.path.basename(db_path))[0]
    percent = int(round(float(min_similarity) * 100))
    run_ts = time.strftime("%Y%m%d_%H%M%S")
    candidate = os.path.join(base_dir, f"{stem}_lowSim_min{percent}_{run_ts}.db")
    new_db = output_db_path or candidate
    if os.path.exists(new_db):
        # 回避极端撞名
        i = 1
        while True:
            alt = os.path.join(base_dir, f"{stem}_lowSim_min{percent}_{run_ts}_{i}.db")
            if not os.path.exists(alt):
                new_db = alt
                break
            i += 1

    conn = sqlite3.connect(new_db)
    try:
        _ensure_db_schema(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_filename ON records(file_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_wos ON records(wos_categories)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_research ON records(research_areas)")
        conn.commit()

        # 附加原库
        conn.execute("ATTACH DATABASE ? AS src_db", (os.path.abspath(db_path),))

        where, params = _build_filter_sql(
            wos_keywords=wos_keywords,
            research_keywords=research_keywords,
            file_name_keywords=file_name_keywords,
            country_keywords=country_keywords,
        )

        base_where = " WHERE " if not where else where + " AND "

        # 插入低相似度记录
        insert_sql = (
            """
            INSERT INTO records (
                file_name, row_index, short_name, country, ethnic_chinese, full_name,
                email, wos_categories, research_areas,
                raw_reprint, raw_email, email_validity, email_validation_attempts, similarity
            )
            SELECT
                r.file_name, r.row_index, r.short_name, r.country, r.ethnic_chinese, r.full_name,
                r.email, r.wos_categories, r.research_areas,
                r.raw_reprint, r.raw_email, r.email_validity, r.email_validation_attempts, COALESCE(r.similarity, 0.0)
            FROM src_db.records AS r
            """
            + base_where
            + (" r.ethnic_chinese IN (" + ",".join(["?"] * len(ethnic_filters)) + ") AND" if ethnic_filters else "")
            + " COALESCE(r.similarity, 0.0) < ?"
        )
        bind_params = list(params)
        if ethnic_filters:
            bind_params.extend(list(ethnic_filters))
        bind_params.append(float(min_similarity))
        conn.execute(insert_sql, bind_params)

        # 复制 errors 表便于上下文追踪
        conn.execute(
            """
            INSERT INTO errors (
                file_name, row_index, reason,
                raw_reprint, raw_email, raw_row
            )
            SELECT file_name, row_index, reason, raw_reprint, raw_email, raw_row
            FROM src_db.errors
            """
        )
        conn.commit()

        cur = conn.execute("SELECT COUNT(*) FROM records")
        kept = int(cur.fetchone()[0])
        conn.execute("DETACH DATABASE src_db")
        return new_db, kept
    finally:
        conn.close()
