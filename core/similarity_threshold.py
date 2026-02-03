from __future__ import annotations

from typing import List, Tuple, Dict

try:
    from core.parsing import _calculate_match_score
except Exception:
    import os, sys
    base = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(base)
    if root not in sys.path:
        sys.path.append(root)
    from core.parsing import _calculate_match_score


def _cases() -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    positives = [
        ("Xu, Cheng", "chengxu@uni.edu"),
        ("Xu, Cheng", "xucheng@uni.edu"),
        ("Yang, Li-Tao", "ltyang@lab.org"),
        ("Yang, Li Tao", "liyang@lab.org"),
        ("Wang, Wei", "wei.wang@dept.cn"),
        ("Wang, Wei", "wangwei@dept.cn"),
        ("Zhou, QY", "qzhou@school.edu"),
        ("Duan, Jinyun", "jinyun.duan@ecnu.cn"),
        ("Lee, J", "jlee@company.com"),
        ("Li, Ming", "ming.li@uni.edu"),
    ]

    negatives = [
        ("Xu, Cheng", "marketing@uni.edu"),
        ("Wang, Wei", "john.smith@dept.cn"),
        ("Yang, Li-Tao", "room101@lab.org"),
        ("Lee, J", "service@company.com"),
        ("Zhou, QY", "finance@school.edu"),
        ("Li, Ming", "support@uni.edu"),
        ("Chen, Yu", "project@team.io"),
    ]
    return positives, negatives


def evaluate_similarity_threshold() -> Dict[str, float]:
    pos, neg = _cases()
    pos_scores = [
        _calculate_match_score(name, email) for name, email in pos
    ]
    neg_scores = [
        _calculate_match_score(name, email) for name, email in neg
    ]

    best_t = 0.0
    best_f1 = -1.0
    best_prec_t = 0.0
    best_prec = -1.0
    best_prec_recall = 0.0

    for k in range(30, 91):
        t = k / 100.0
        tp = sum(1 for s in pos_scores if s >= t)
        fn = sum(1 for s in pos_scores if s < t)
        fp = sum(1 for s in neg_scores if s >= t)
        tn = sum(1 for s in neg_scores if s < t)

        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * prec * rec) / (prec + rec) if (prec + rec) > 0 else 0.0

        if f1 > best_f1:
            best_f1 = f1
            best_t = t

        if prec > best_prec or (prec == best_prec and rec > best_prec_recall):
            best_prec = prec
            best_prec_recall = rec
            best_prec_t = t

    return {
        "best_f1_threshold": best_t,
        "best_f1": best_f1,
        "best_precision_threshold": best_prec_t,
        "best_precision": best_prec,
        "best_precision_recall": best_prec_recall,
        "pos_min": min(pos_scores) if pos_scores else 0.0,
        "pos_max": max(pos_scores) if pos_scores else 0.0,
        "neg_min": min(neg_scores) if neg_scores else 0.0,
        "neg_max": max(neg_scores) if neg_scores else 0.0,
    }


if __name__ == "__main__":
    stats = evaluate_similarity_threshold()
    print(stats)
