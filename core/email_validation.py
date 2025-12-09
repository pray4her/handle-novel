from __future__ import annotations

import os
import re
import smtplib
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Tuple, Sequence, Optional

import dns.exception
import dns.resolver

from .concurrency import _ensure_db_schema
from .exporter import classify_ethnic_chinese


ProgressCallback = Callable[[Dict[str, Any]], None] | None


_VALID_LOCAL_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "!#$%&'*+/=?^_`{|}~.-"
)

_PROXY_ENV_VARS = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
]


@contextmanager
def _no_proxy_env() -> Any:
    """
    临时移除常见代理相关环境变量，避免 DNS/SMTP 调用误读 HTTP/SOCKS 代理配置。

    注意：若本机使用 TUN 模式（如 v2rayN TUN），底层路由仍由系统控制，
    此处只能保证不使用基于环境变量的应用层代理。
    """
    backup: Dict[str, str] = {}
    for key in _PROXY_ENV_VARS:
        if key in os.environ:
            backup[key] = os.environ.pop(key)
    try:
        yield
    finally:
        os.environ.update(backup)


def is_email_syntax_valid(email: str) -> Tuple[bool, str]:
    """
    第一层：邮箱语法格式校验。

    返回 (是否通过, 失败原因代码)。
    """
    if email is None:
        return False, "empty"

    email = str(email).strip()
    if not email:
        return False, "empty"

    if len(email) > 254:
        return False, "too_long_total"

    # 禁止空格和非 ASCII 字符
    for ch in email:
        if ch.isspace():
            return False, "contains_space"
        if ord(ch) < 33 or ord(ch) > 126:
            return False, "non_ascii_char"

    if email.count("@") != 1:
        return False, "at_count"

    local, domain = email.split("@", 1)
    if not local or not domain:
        return False, "empty_local_or_domain"

    if len(local) > 64:
        return False, "too_long_local"

    # local 部分字符集合检查
    for ch in local:
        if ch not in _VALID_LOCAL_CHARS:
            return False, "invalid_local_char"

    if local[0] == "." or local[-1] == ".":
        return False, "local_dot_edge"
    if ".." in local:
        return False, "local_double_dot"

    # domain 基础检查
    if domain[0] == "." or domain[-1] == ".":
        return False, "domain_dot_edge"
    if ".." in domain:
        return False, "domain_double_dot"
    if not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
        return False, "domain_invalid_char"

    labels = domain.split(".")
    if len(labels) < 2:
        return False, "domain_label_count"

    for label in labels:
        if not label:
            return False, "domain_empty_label"
        if len(label) > 63:
            return False, "domain_label_too_long"
        if not re.fullmatch(r"[A-Za-z0-9-]+", label):
            return False, "domain_label_invalid_char"
        if label[0] == "-" or label[-1] == "-":
            return False, "domain_label_hyphen_edge"

    return True, "ok"


def _extract_domain(email: str) -> str:
    try:
        return str(email).rsplit("@", 1)[1].strip().lower()
    except Exception:
        return ""


def check_domain_dns(domain: str, timeout: float = 3.0) -> Tuple[str, List[str]]:
    """
    第二层：DNS & MX 记录校验。

    返回 (状态, 主机列表)：
        - \"mx\"      : 找到 MX 记录，主机列表为 MX 主机（按优先级排序）。
        - \"a_only\"  : 无 MX，但存在 A/AAAA 记录，主机列表为 IP。
        - \"nx_domain\": 域名不存在。
        - \"error\"   : 解析失败或超时。
    """
    if not domain:
        return "error", []

    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout

    hosts: List[str] = []

    # 先查 MX
    try:
        answers = resolver.resolve(domain, "MX")
        mx_records = sorted(
            (
                (int(r.preference), str(r.exchange).rstrip("."))
                for r in answers
            ),
            key=lambda x: x[0],
        )
        hosts = [h for _, h in mx_records]
        if hosts:
            return "mx", hosts
    except dns.resolver.NXDOMAIN:
        return "nx_domain", []
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        # 域存在但没有 MX，后续尝试 A/AAAA
        pass
    except dns.exception.Timeout:
        return "error", []
    except dns.exception.DNSException:
        return "error", []

    # 再查 A / AAAA
    try:
        answers_a = resolver.resolve(domain, "A")
        hosts.extend(str(r.address) for r in answers_a)
    except dns.resolver.NXDOMAIN:
        return "nx_domain", []
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        pass
    except dns.exception.Timeout:
        return "error", []
    except dns.exception.DNSException:
        return "error", []

    try:
        answers_aaaa = resolver.resolve(domain, "AAAA")
        hosts.extend(str(r.address) for r in answers_aaaa)
    except dns.resolver.NXDOMAIN:
        return "nx_domain", []
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        pass
    except dns.exception.Timeout:
        return "error", []
    except dns.exception.DNSException:
        return "error", []

    if hosts:
        return "a_only", hosts

    return "error", []


def check_smtp_deliverable(
    email: str,
    mail_from: str,
    hosts: List[str],
    timeout: float = 15.0,
    max_retries: int = 2,
) -> Tuple[str, str]:
    """
    第三层：SMTP 握手验证。

    返回 (状态, 详细原因)：
        - \"valid\"   : 服务器返回 2xx，邮箱存在。
        - \"invalid\" : 服务器返回 5xx，邮箱不存在/被拒绝。
        - \"unknown\" : 连接超时、4xx 临时错误等不确定情况。
    """
    email = str(email).strip()
    mail_from = str(mail_from).strip()

    if not hosts:
        return "unknown", "no_smtp_hosts"

    last_reason = "smtp_unknown"
    hostname = socket.getfqdn() or "localhost"

    total_attempts = max(max_retries, 0) + 1

    for host in hosts:
        for attempt in range(total_attempts):
            try:
                with smtplib.SMTP(host=host, port=25, timeout=timeout) as smtp:
                    smtp.set_debuglevel(0)
                    try:
                        code, _ = smtp.ehlo()
                        if 400 <= code < 600:
                            code, _ = smtp.helo(hostname)
                    except smtplib.SMTPHeloError:
                        # 视为临时失败，尝试重试或切换主机
                        last_reason = "smtp_helo_error"
                        continue

                    # 若服务器支持 STARTTLS，可尝试升级以提高兼容性
                    if smtp.has_extn("starttls"):
                        try:
                            smtp.starttls()
                            smtp.ehlo()
                        except smtplib.SMTPException:
                            # TLS 协商失败不直接判定为无效，继续尝试后续流程或重试
                            last_reason = "smtp_starttls_failed"

                    code, _ = smtp.mail(mail_from)
                    if 400 <= code < 600:
                        # 发件人被拒绝，通常与 IP / 发件域信誉相关，记为不确定
                        last_reason = f"mail_from_rejected_{code}"
                        continue

                    code, _ = smtp.rcpt(email)
                    if 200 <= code < 300:
                        return "valid", f"smtp_{code}"
                    if 500 <= code < 600:
                        return "invalid", f"smtp_{code}"
                    if 400 <= code < 500:
                        last_reason = f"smtp_temp_{code}"
                        # 临时错误，后续重试或切换主机
                    else:
                        last_reason = f"smtp_other_{code}"
            except (
                smtplib.SMTPConnectError,
                smtplib.SMTPServerDisconnected,
                smtplib.SMTPHeloError,
                smtplib.SMTPDataError,
                smtplib.SMTPRecipientsRefused,
                smtplib.SMTPResponseException,
                socket.timeout,
                OSError,
            ) as e:
                last_reason = f"exception_{type(e).__name__}"

        # 当前主机多次尝试后仍失败，切换到下一个主机

    return "unknown", last_reason


def validate_email(
    email: str,
    mail_from: str,
    mx_timeout: float = 3.0,
    smtp_timeout: float = 15.0,
    max_retries: int = 2,
) -> Tuple[str, str]:
    """
    统一邮箱验证入口：语法 + DNS/MX + SMTP。

    返回 (中文状态, 内部原因码)：
        - \"有效\"
        - \"无效\"
        - \"不可验证\"
    """
    try:
        ok, reason = is_email_syntax_valid(email)
        if not ok:
            return "无效", f"syntax_{reason}"

        domain = _extract_domain(email)
        status, hosts = check_domain_dns(domain, timeout=mx_timeout)

        if status == "nx_domain":
            return "无效", "dns_nx_domain"
        if status == "error":
            return "不可验证", "dns_error"
        if not hosts:
            return "不可验证", "dns_no_hosts"

        smtp_status, smtp_reason = check_smtp_deliverable(
            email=email,
            mail_from=mail_from,
            hosts=hosts,
            timeout=smtp_timeout,
            max_retries=max_retries,
        )

        if smtp_status == "valid":
            return "有效", smtp_reason
        if smtp_status == "invalid":
            return "无效", smtp_reason
        return "不可验证", smtp_reason
    except Exception as e:
        # 兜底保护，避免抛出异常中断大批量任务
        return "不可验证", f"internal_error_{type(e).__name__}"


def _build_email_filter_sql(
    wos_keywords: Sequence[str] | None,
    research_keywords: Sequence[str] | None,
    file_name_keywords: Sequence[str] | None,
    country_keywords: Sequence[str] | None,
) -> Tuple[str, List[str]]:
    """
    为邮箱验证构建与导出共享的过滤条件（不包含“华人华裔”分类）。

    返回 (追加到 WHERE 后的 SQL 片段, 参数列表)，SQL 片段要么为空字符串，要么以 AND 开头。
    """
    conditions: List[str] = []
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

    if not conditions:
        return "", []

    where_suffix = " AND " + " AND ".join(conditions)
    return where_suffix, params


def validate_all_emails_in_db(
    db_path: str,
    mail_from: str,
    mx_timeout: float,
    smtp_timeout: float,
    concurrency: int,
    max_retries: int,
    retry_times: int = 0,
    wos_keywords: Sequence[str] | None = None,
    research_keywords: Sequence[str] | None = None,
    file_name_keywords: Sequence[str] | None = None,
    country_keywords: Sequence[str] | None = None,
    ethnic_filters: Sequence[str] | None = None,
    progress_callback: ProgressCallback = None,
) -> None:
    """
    批量验证 SQLite 数据库中邮箱的有效性，并更新 records.email_validity 字段。

    约定：
        - 仅对 email 非空，且 email_validity 为 NULL 或 “不可验证” 的邮箱重新验证；
        - email_validity 最终值：
            * NULL       : 未验证
            * “有效”     : 通过三层验证
            * “无效”     : 语法或服务器明确拒绝
            * “不可验证” : 超时 / 临时错误 / 网络故障等不确定情况
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite 数据库不存在：{db_path}")

    if concurrency <= 0:
        concurrency = 1

    with _no_proxy_env():
        # 先读取待验证邮箱列表（在记录层面应用过滤条件，再聚合到邮箱级别）
        conn = sqlite3.connect(db_path)
        try:
            _ensure_db_schema(conn)
            cur = conn.cursor()
            # 构建与导出共享的基础过滤（不含“华人华裔”）
            where_suffix, params = _build_email_filter_sql(
                wos_keywords=wos_keywords,
                research_keywords=research_keywords,
                file_name_keywords=file_name_keywords,
                country_keywords=country_keywords,
            )
            sql = (
                """
                SELECT
                    email,
                    COALESCE(email_validation_attempts, 0) AS attempts,
                    short_name,
                    full_name,
                    country
                FROM records
                WHERE email IS NOT NULL
                  AND email <> ''
                  AND (email_validity IS NULL OR email_validity = '不可验证')
                """
                + where_suffix
            )
            cur.execute(sql, params)
            rows = cur.fetchall()

            # 先按邮箱聚合 attempts，后续再按“华人华裔”过滤邮箱集合
            attempts_by_email: Dict[str, int] = {}
            meta_by_email: Dict[str, List[Tuple[str, str, str]]] = {}
            for email, attempts, short_name, full_name, country in rows:
                addr_str = str(email).strip()
                if not addr_str:
                    continue
                current_attempts = int(attempts or 0)
                prev = attempts_by_email.get(addr_str)
                if prev is None or current_attempts > prev:
                    attempts_by_email[addr_str] = current_attempts
                meta_by_email.setdefault(addr_str, []).append(
                    (
                        str(short_name or ""),
                        str(full_name or ""),
                        str(country or ""),
                    )
                )

            # 依据“华人华裔”过滤邮箱：只有当某邮箱至少一条记录满足分类条件时才参与验证
            ethnic_set: Optional[set[str]]
            if ethnic_filters:
                ethnic_set = {str(x).strip() for x in ethnic_filters if str(x).strip()}
            else:
                ethnic_set = None

            emails: List[Tuple[str, int]] = []
            for addr_str, current_attempts in attempts_by_email.items():
                if ethnic_set:
                    records = meta_by_email.get(addr_str, [])
                    matched = False
                    for short_name, full_name, country in records:
                        cls = classify_ethnic_chinese(
                            full_name=full_name,
                            short_name=short_name,
                            country=country,
                        )
                        if cls in ethnic_set:
                            matched = True
                            break
                    if not matched:
                        continue
                emails.append((addr_str, current_attempts))
        finally:
            conn.close()

        total = len(emails)
        if progress_callback:
            progress_callback(
                {"type": "email_validation_start", "total_emails": total}
            )

        if total == 0:
            if progress_callback:
                progress_callback(
                    {
                        "type": "email_validation_done",
                        "total": 0,
                        "valid": 0,
                        "invalid": 0,
                        "unknown": 0,
                    }
                )
            return

        conn_u = sqlite3.connect(db_path)
        try:
            _ensure_db_schema(conn_u)
            cur_u = conn_u.cursor()

            completed = 0
            n_valid = 0
            n_invalid = 0
            n_unknown = 0
            batch: List[Tuple[str, str]] = []
            batch_size = 100

            # 为单次任务内的自动重试做保护，避免异常配置导致无限循环
            if retry_times < 0:
                retry_times = 0

            def _validate_one(addr: str) -> Tuple[str, str]:
                """
                针对单个邮箱做自动重试：
                    - 至少验证 1 次；
                    - 若结果为“不可验证”，在当前任务内最多再重试 retry_times 次；
                    - 一旦得到“有效”或“无效”，立即返回。
                """
                attempts_left = retry_times + 1  # 首次 + 重试次数
                last_status = "不可验证"
                last_reason = "not_started"

                while attempts_left > 0:
                    attempts_left -= 1
                    status, reason = validate_email(
                        email=addr,
                        mail_from=mail_from,
                        mx_timeout=mx_timeout,
                        smtp_timeout=smtp_timeout,
                        max_retries=max_retries,
                    )
                    last_status, last_reason = status, reason

                    # 只要不是“不可验证”，立即结束重试
                    if status != "不可验证":
                        break

                return last_status, last_reason

            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                future_to_email: Dict[Any, Tuple[str, int]] = {
                    executor.submit(_validate_one, addr): (addr, attempts)
                    for (addr, attempts) in emails
                }

                for future in as_completed(future_to_email):
                    email_addr, prev_attempts = future_to_email[future]
                    try:
                        status, _reason = future.result()
                    except Exception:
                        status = "不可验证"

                    if status == "有效":
                        n_valid += 1
                    elif status == "无效":
                        n_invalid += 1
                    else:
                        n_unknown += 1
                        status = "不可验证"

                    completed += 1
                    batch.append((status, email_addr))

                    if len(batch) >= batch_size:
                        cur_u.executemany(
                            """
                            UPDATE records
                            SET email_validity = ?,
                                email_validation_attempts = COALESCE(email_validation_attempts, 0) + 1
                            WHERE email = ?
                            """,
                            batch,
                        )
                        conn_u.commit()
                        batch.clear()

                    if progress_callback and (
                        completed == total or completed % 20 == 0
                    ):
                        progress_callback(
                            {
                                "type": "email_validation_progress",
                                "completed": completed,
                                "total": total,
                                "valid": n_valid,
                                "invalid": n_invalid,
                                "unknown": n_unknown,
                                "sample_email": email_addr,
                                "status": status,
                            }
                        )

            if batch:
                cur_u.executemany(
                    """
                    UPDATE records
                    SET email_validity = ?,
                        email_validation_attempts = COALESCE(email_validation_attempts, 0) + 1
                    WHERE email = ?
                    """,
                    batch,
                )
                conn_u.commit()

        finally:
            conn_u.close()

        if progress_callback:
            progress_callback(
                {
                    "type": "email_validation_done",
                    "total": total,
                    "valid": n_valid,
                    "invalid": n_invalid,
                    "unknown": n_unknown,
                }
            )


