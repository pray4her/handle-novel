import re
import difflib
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple


class AlignmentError(Exception):
    """作者与邮箱对齐失败时抛出的业务异常。"""


class AuthorContact(NamedTuple):
    """对齐后的通讯作者条目。"""

    short_name: str
    email: str
    country: str
    full_name: Optional[str] = None  # 当使用 Addresses 列时，可直接存储完整名字


def _normalize_whitespace(text: str) -> str:
    """压缩多余空白，便于比较与展示。"""
    return " ".join(str(text).split())


def normalize_short_name_key(short_name: str) -> str:
    """
    将作者简称标准化为用于去重的 key。

    规则：只保留英文字母并大写，例如
    "Zhou, QY" -> "ZHOUQY"
    """
    if not short_name:
        return ""
    return re.sub(r"[^A-Z]", "", str(short_name).upper())


def extract_short_name_from_reprint_segment(segment: str) -> str:
    """
    从通讯作者地址片段中提取作者简称。

    示例：
    "Zhou, QY (Shanghai Univ of ...)" -> "Zhou, QY"
    """
    if not segment:
        return ""

    text = str(segment)
    # 只取第一个括号前的部分，一般为 "姓, 名缩写"
    head = text.split("(", 1)[0].strip()
    # 去掉末尾多余的标点与空白
    head = re.sub(r"[.,;:\s]+$", "", head)

    if "," in head:
        # 已经是 "姓, 名缩写" 形式
        surname, rest = head.split(",", 1)
        surname = surname.strip()
        rest = rest.strip()
        short = f"{surname}, {rest}" if rest else surname
    else:
        # 退化处理：第一个词视为姓，后续词取首字母作为缩写
        tokens = head.split()
        if len(tokens) >= 2:
            surname = tokens[0]
            initials = "".join(tok[0] for tok in tokens[1:] if tok)
            short = f"{surname}, {initials}"
        else:
            short = head

    return _normalize_whitespace(short)


def _has_address_details(segment: str) -> bool:
    """判断通讯作者片段是否包含姓名之外的地址信息。"""
    if not segment:
        return False
    text = re.sub(r"\([^)]*\)", " ", str(segment))
    text = text.strip()
    if "," not in text:
        return False
    _, rest = text.split(",", 1)
    rest = rest.strip()
    if not rest:
        return False
    # 如果剩余部分只有 1~3 个字母（通常是姓名缩写），视为无地址
    letters_only = re.sub(r"[^A-Za-z]", "", rest)
    if len(rest) <= 4 and letters_only.isalpha() and len(letters_only) <= 3:
        return False
    # 若仍然没有空格或数字，说明缺少进一步的地址信息
    if not re.search(r"[0-9\s]", rest):
        return False
    return True


def extract_country_from_reprint_segment(segment: str) -> str:
    """
    尝试从通讯作者地址片段末尾提取国家/地区（例如 "Peoples R China"）。

    解析策略：
        1. 去掉括号中的机构详情；
        2. 将 ';' 和 '.' 视为分隔符统一替换成 ','；
        3. 以 ',' 拆分后逆序遍历，找到最后一个包含字母的字段；
        4. 去掉其中的邮编等数字，只保留字母/空白/连字符。
    """
    if not segment:
        return ""

    text = str(segment)
    text = re.sub(r"\([^)]*\)", " ", text)  # 移除括号内容
    text = text.replace(";", ",").replace(".", ",")
    text = " ".join(text.split())
    text = text.strip(" ,")

    parts = [part.strip() for part in text.split(",") if part.strip()]
    for part in reversed(parts):
        # 去除数字与多余符号，仅保留字母及必要符号
        cleaned = re.sub(r"[0-9]", " ", part)
        cleaned = re.sub(r"[^A-Za-z\u00C0-\u024F\u2E80-\u9FFF'&\s-]", " ", cleaned)
        cleaned = " ".join(cleaned.split()).strip(" ,")
        if cleaned and any(ch.isalpha() for ch in cleaned):
            tokens = cleaned.split()
            if (
                len(tokens) >= 2
                and tokens[-1].isupper()
                and len(tokens[-1]) <= 3
            ):
                return tokens[-1]
            return cleaned
    return ""


def _calculate_match_score(author_name: str, email: str) -> float:
    """
    计算作者姓名与邮箱的相似度。

    增强策略：同时考虑常见的邮箱命名模式，例如
        - 姓在前/名在后："xu" + "cheng" -> "xucheng"
        - 名在前/姓在后："cheng" + "xu" -> "chengxu"
        - 名字首字母 + 姓："lt" + "yang" -> "ltyang"
        - 姓 + 名字首字母："yang" + "lt" -> "yanglt"
    最终得分取以上模式与基础完整名的最大值。
    """
    if not author_name or not email:
        return 0.0

    email_prefix = email.split("@")[0]

    def normalize_letters_digits(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(s).lower())

    def normalize_letters(s: str) -> str:
        return re.sub(r"[^a-z]", "", str(s).lower())

    norm_email = normalize_letters_digits(email_prefix)
    if not norm_email:
        return 0.0

    base_name = normalize_letters_digits(author_name)
    base_score = difflib.SequenceMatcher(None, base_name, norm_email).ratio() if base_name else 0.0

    # 拆解姓名为姓与名词组（按逗号或空格）
    text = _normalize_whitespace(str(author_name))
    surname = ""
    given_tokens: List[str] = []
    if "," in text:
        left, right = text.split(",", 1)
        surname = normalize_letters(left)
        given_tokens = [normalize_letters(tok) for tok in right.split() if normalize_letters(tok)]
    else:
        words = [normalize_letters(w) for w in text.split() if normalize_letters(w)]
        if words:
            surname = words[-1]
            given_tokens = words[:-1]

    variants: List[str] = []
    if surname:
        joined_given = "".join(given_tokens)
        initials = "".join(tok[0] for tok in given_tokens if tok)
        if joined_given:
            variants.append(surname + joined_given)
            variants.append(joined_given + surname)
        if initials:
            variants.append(initials + surname)
            variants.append(surname + initials)

    # 计算所有变体的得分，取最大
    scores = [base_score]
    for v in variants:
        if not v:
            continue
        scores.append(difflib.SequenceMatcher(None, v, norm_email).ratio())

    return max(scores) if scores else 0.0


def _split_segments_outside_brackets(text: str) -> List[str]:
    """
    将包含多个地址段的字符串按分号切分，但忽略方括号内的分号。

    例如：
        "[A; B] Inst, Country; [C] Inst, Country" ->
        ["[A; B] Inst, Country", "[C] Inst, Country"]
    """
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    for ch in str(text):
        if ch == "[":
            depth += 1
        elif ch == "]" and depth > 0:
            depth -= 1
        if ch == ";" and depth == 0:
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def extract_authors_from_addresses(addresses_str: str) -> List[Tuple[str, str]]:
    """
    从 Addresses 列中提取作者姓名和国家信息。
    
    格式示例：
        [Duan, Jinyun] East China Normal Univ, Shanghai, Peoples R China; 
        [Zong, Zhaobiao] Huaibei Normal Univ, Huaibei, Peoples R China
    
    返回：
        [(姓名, 国家), ...] 列表
    """
    if not addresses_str or str(addresses_str).strip() == "":
        return []
    
    text = str(addresses_str)
    # 仅在方括号外切分段，避免误将括号内多个作者拆成不同段
    segments = _split_segments_outside_brackets(text)
    
    authors = []
    for segment in segments:
        # 提取方括号中的所有作者姓名
        # 格式：[Name1; Name2; Name3] 或 [Name]
        bracket_match = re.search(r'\[([^\]]+)\]', segment)
        if not bracket_match:
            continue
        
        names_str = bracket_match.group(1)
        # 方括号内可能有多个作者，用分号或逗号分隔
        # 优先按分号分隔
        if ';' in names_str:
            names = [n.strip() for n in names_str.split(';') if n.strip()]
        else:
            # 如果没有分号，只有一个作者
            names = [names_str.strip()]
        
        # 提取该地址段的国家信息
        country = extract_country_from_reprint_segment(segment)
        
        # 为每个作者创建条目
        for name in names:
            if name:
                authors.append((name, country))
    
    return authors


def _dedupe_addresses_authors(authors: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """
    按作者名去重，保留原始出现顺序，并合并国家信息：
        - key：作者名（归一化空白并大写）
        - 国家优先保留非空值；若多次出现国家不同，选择更“信息丰富”的（长度更长）
    """
    seen: Dict[str, Tuple[str, str]] = {}
    order: List[str] = []
    for name, country in authors:
        key = _normalize_whitespace(name).upper()
        if key not in seen:
            seen[key] = (name, country)
            order.append(key)
        else:
            old_name, old_country = seen[key]
            new_country = old_country
            if not old_country and country:
                new_country = country
            elif old_country and country and len(country) > len(old_country):
                new_country = country
            seen[key] = (old_name, new_country)
    return [seen[k] for k in order]


def parse_and_align_authors(
    reprint_str: str,
    email_str: str,
    addresses_str: Optional[str] = None,
) -> List[AuthorContact]:
    """
    解析通讯作者地址串与邮箱串，并进行去重对齐。

    参数：
        reprint_str: 通讯作者地址列的原始字符串（以 ';' 分隔）。
        email_str: 邮箱列的原始字符串（以 ';' 分隔）。
        addresses_str: 地址列的原始字符串（可选），包含格式如 [Name] Institution。

    返回：
        列表 [(short_name, email), ...]，按相似度匹配。
    """
    if reprint_str is None or str(reprint_str).strip() == "":
        raise AlignmentError("通讯作者为空")
    if email_str is None or str(email_str).strip() == "":
        raise AlignmentError("邮箱为空")

    # Reprint 列按分号切分时忽略括号内的分号，避免机构详情中的 ';' 造成误切
    def _split_segments_outside_parens(text: str) -> List[str]:
        parts: List[str] = []
        buf: List[str] = []
        depth = 0
        for ch in str(text):
            if ch == "(":
                depth += 1
            elif ch == ")" and depth > 0:
                depth -= 1
            if ch == ";" and depth == 0:
                seg = "".join(buf).strip()
                if seg:
                    parts.append(seg)
                buf = []
            else:
                buf.append(ch)
        tail = "".join(buf).strip()
        if tail:
            parts.append(tail)
        return parts

    reprint_segments = [seg.strip() for seg in _split_segments_outside_parens(str(reprint_str)) if seg.strip()]
    email_list = [em.strip() for em in str(email_str).split(";") if em.strip()]

    if not reprint_segments:
        raise AlignmentError("没有有效的通讯作者地址段")
    if not email_list:
        raise AlignmentError("没有有效的邮箱地址")

    # 若某个片段只有姓名没有地址，则借用下一个包含地址信息的片段用于国家提取
    resolved_segments: List[str] = []
    for idx, seg in enumerate(reprint_segments):
        if _has_address_details(seg):
            resolved_segments.append(seg)
            continue
        fallback = ""
        for next_seg in reprint_segments[idx + 1 :]:
            if _has_address_details(next_seg):
                fallback = next_seg
                break
        resolved_segments.append(fallback or seg)

    author_entries = []
    for orig_seg, addr_seg in zip(reprint_segments, resolved_segments):
        short = extract_short_name_from_reprint_segment(orig_seg)
        country = extract_country_from_reprint_segment(addr_seg)
        author_entries.append((short, country))

    n_auth = len(author_entries)
    n_email = len(email_list)

    # 场景 A：单邮箱场景，使用 Reprint Addresses 列
    if n_email == 1:
        # 单作者单邮箱，直接返回
        if n_auth == 1:
            short, country = author_entries[0]
            return [
                AuthorContact(short_name=short, email=email_list[0], country=country, full_name=None)
            ]
        
        # 多作者单邮箱，选择相似度最高的
        best_idx = 0
        best_score = _calculate_match_score(author_entries[0][0], email_list[0])
        for i in range(1, n_auth):
            score = _calculate_match_score(author_entries[i][0], email_list[0])
            if score > best_score:
                best_score = score
                best_idx = i
        
        short, country = author_entries[best_idx]
        return [
            AuthorContact(short_name=short, email=email_list[0], country=country, full_name=None)
        ]

    # 场景 B：多邮箱场景
    # 优先使用 Addresses 列（如果存在且不为空）
    addresses_authors = []
    if addresses_str:
        addresses_authors = extract_authors_from_addresses(addresses_str)
    
    # 如果 Addresses 列有效且有数据，使用它进行匹配
    if addresses_authors:
        # 使用 Addresses 列前，先按作者名去重，避免同名多次匹配到不同邮箱
        addresses_authors = _dedupe_addresses_authors(addresses_authors)
        # 使用 Addresses 列的作者名称进行匹配
        matches = []
        for i, (name, country) in enumerate(addresses_authors):
            for j, email in enumerate(email_list):
                score = _calculate_match_score(name, email)
                matches.append((score, i, j))
        
        # 按相似度降序排序
        matches.sort(key=lambda x: x[0], reverse=True)
        
        used_authors = set()
        used_emails = set()
        matched_pairs = []
        
        # 贪心匹配
        for score, auth_idx, email_idx in matches:
            if auth_idx in used_authors or email_idx in used_emails:
                continue
            
            used_authors.add(auth_idx)
            used_emails.add(email_idx)
            matched_pairs.append((auth_idx, email_idx))
        
        # 按作者原本顺序排序
        matched_pairs.sort(key=lambda x: x[0])
        
        # 当使用 Addresses 列时，直接将其中的名字作为 full_name
        return [
            AuthorContact(
                short_name=addresses_authors[ai][0],
                email=email_list[ei],
                country=addresses_authors[ai][1],
                full_name=addresses_authors[ai][0]  # 直接使用 Addresses 列中的名字作为 full_name
            )
            for ai, ei in matched_pairs
        ]
    
    # 否则回退到使用 Reprint Addresses 列
    matches = []
    for i, (short, _) in enumerate(author_entries):
        for j, email in enumerate(email_list):
            score = _calculate_match_score(short, email)
            matches.append((score, i, j))

    # 按相似度降序排序
    matches.sort(key=lambda x: x[0], reverse=True)

    used_authors = set()
    used_emails = set()
    matched_pairs = []

    # 贪心匹配
    for score, auth_idx, email_idx in matches:
        if auth_idx in used_authors or email_idx in used_emails:
            continue

        used_authors.add(auth_idx)
        used_emails.add(email_idx)
        matched_pairs.append((auth_idx, email_idx))

    # 按作者原本顺序排序
    matched_pairs.sort(key=lambda x: x[0])

    return [
        AuthorContact(
            short_name=author_entries[ai][0],
            email=email_list[ei],
            country=author_entries[ai][1],
            full_name=None  # 使用 Reprint Addresses 列时，full_name 将通过后续的 map_short_to_full_names 填充
        )
        for ai, ei in matched_pairs
    ]


def _split_semicolon_field(value: str) -> List[str]:
    """按分号切分并去除首尾空白，过滤空项。"""
    if value is None:
        return []
    return [part.strip() for part in str(value).split(";") if part.strip()]


def normalize_semicolon_tags(value: str) -> str:
    """
    规范化以分号分隔的标签字段：
        - 按 ';' 切分；
        - 去除首尾空白和空项；
        - 使用统一格式 '; ' 拼接。

    示例：
        "Business;  Psychology, Applied ;Management;"
        -> "Business; Psychology, Applied; Management"
    """
    parts = _split_semicolon_field(value)
    if not parts:
        return ""
    return "; ".join(parts)


def _normalize_person_name_for_match(name: str) -> Tuple[str, str]:
    """
    将简称（通常含逗号）归一化为 (姓, 名字首字母串)。
    """
    if not name:
        return "", ""

    text = _normalize_whitespace(str(name))

    if "," in text:
        surname, rest = text.split(",", 1)
        surname = surname.strip().upper()
        initials = "".join(ch for ch in rest if ch.isalpha()).upper()
        return surname, initials

    tokens = text.split()
    if not tokens:
        return "", ""

    surname = tokens[-1].upper()
    given_tokens = tokens[:-1]
    initials = "".join(tok[0].upper() for tok in given_tokens if tok)
    return surname, initials


def _possible_surnames_from_full_name(name: str) -> List[str]:
    """
    返回全名可能的姓氏候选：
        - 含逗号：仅取逗号前；
        - 无逗号：既取最后一个词，也取第一个词（处理“Jing Junfeng”这类东亚姓名）。
    """
    if not name:
        return []

    text = _normalize_whitespace(str(name))
    surnames: List[str] = []

    if "," in text:
        surname = text.split(",", 1)[0].strip().upper()
        if surname:
            surnames.append(surname)
        return surnames

    tokens = text.split()
    if not tokens:
        return []

    last = tokens[-1].upper()
    if last:
        surnames.append(last)
    if len(tokens) >= 2:
        first = tokens[0].upper()
        if first and first not in surnames:
            surnames.append(first)
    return surnames


def _extract_initial_for_assumed_surname(full_name: str, assumed_surname: str) -> str:
    """
    在假定姓氏为 assumed_surname 的情况下，返回全名的名字首字母。
    """
    if not full_name or not assumed_surname:
        return ""

    assumed = assumed_surname.upper()
    text = _normalize_whitespace(str(full_name))

    if "," in text:
        surname, rest = text.split(",", 1)
        if surname.strip().upper() == assumed:
            for ch in rest:
                if ch.isalpha():
                    return ch.upper()
            return ""

    tokens = text.split()
    if not tokens:
        return ""

    tokens_upper = [tok.upper() for tok in tokens]
    given_tokens = []

    if tokens_upper[0] == assumed:
        given_tokens = tokens[1:]
    elif tokens_upper[-1] == assumed:
        given_tokens = tokens[:-1]
    else:
        given_tokens = [tok for tok in tokens if tok.upper() != assumed]

    for tok in given_tokens:
        for ch in tok:
            if ch.isalpha():
                return ch.upper()
    return ""


def map_short_to_full_names(
    short_names: Sequence[str],
    author_full_names_str: str,
) -> Dict[str, Optional[str]]:
    """
    将作者简称列表映射到“作者全称”列中的姓名。

    策略：
        1. 按 ';' 切分作者全称；
        2. 先用 (姓 + 首字母) 精确匹配；
        3. 若无精确匹配、但同姓只有一人，则退化为仅按姓匹配；
        4. 若仍无法区分，则返回 None。
    """
    full_names = _split_semicolon_field(author_full_names_str)
    result: Dict[str, Optional[str]] = {}

    # 预分组：按姓聚合全名（包含可能的首/尾姓氏）
    surname_groups: Dict[str, List[str]] = {}
    for full in full_names:
        for surname in _possible_surnames_from_full_name(full):
            surname_groups.setdefault(surname, []).append(full)

    for short in short_names:
        s_surname, s_initials = _normalize_person_name_for_match(short)
        if not s_surname:
            result[short] = None
            continue

        candidates = surname_groups.get(s_surname, [])
        if not candidates:
            result[short] = None
            continue

        chosen: Optional[str] = None

        # 第一步：姓 + 首字母匹配
        if s_initials:
            first_init = s_initials[0]
            for full in candidates:
                cand_init = _extract_initial_for_assumed_surname(full, s_surname)
                if cand_init == first_init:
                    chosen = full
                    break

        # 第二步：只有一个同姓候选人时，退化为仅按姓匹配
        if chosen is None and len(candidates) == 1:
            chosen = candidates[0]

        result[short] = chosen

    return result
