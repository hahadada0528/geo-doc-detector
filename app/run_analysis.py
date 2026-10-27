"""单次检测跑次结束后的 GEO 汇总分析。"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from app.keywords import analyze_keyword_signals, tokenize

# 正负面词表（简版，围绕品牌口碑/产品表述）
_POS = [
    "好", "优", "强", "推荐", "值得", "满意", "靠谱", "出色", "领先", "提升",
    "稳定", "舒适", "实惠", "划算", "优秀", "出色", "口碑好", "好评", "首选",
    "适合", "专业", "可靠", "精品", "旗舰", "高性价比", "性价比高", "喜欢",
    "不错", "很好", "更好", "最佳", "顶级", "优质", "保障", "安心",
]
_NEG = [
    "差", "糟", "坑", "贵", "难", "弱", "失望", "后悔", "投诉", "故障",
    "问题", "缺陷", "吐槽", "差评", "不值", "拉胯", "翻车", "鸡肋", "一般",
    "糟糕", "不行", "不好", "不如", "落后", "虚假", "夸大", "避雷", "慎买",
    "糟糕", "难受", "卡顿", "漏电", "异味", "噪音大",
]


def _clip(n: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return round(max(lo, min(hi, n)), 1)


def _brand_terms(brand_name: str, aliases: list[str] | None = None) -> list[str]:
    terms = [brand_name.strip()] if brand_name and brand_name.strip() else []
    for a in aliases or []:
        a = (a or "").strip()
        if a and a not in terms:
            terms.append(a)
    return [t for t in terms if t]


def _mentions_brand(text: str, terms: list[str]) -> bool:
    if not text or not terms:
        return False
    low = text.lower()
    for t in terms:
        if t.lower() in low:
            return True
    return False


def _sentiment_score(texts: list[str], terms: list[str]) -> tuple[float, str, dict[str, Any]]:
    """仅在「提到该品牌」的上下文里统计正负面；未提及品牌的文本不计入。

    返回 0–100（50 中性）、标签、明细。
    """
    pos_hits = 0
    neg_hits = 0
    windows: list[str] = []
    brand_mention_windows = 0
    skipped_no_brand = 0

    if not terms:
        return 50.0, "中性 / 未配置品牌", {
            "positive_hits": 0,
            "negative_hits": 0,
            "brand_mention_windows": 0,
            "skipped_no_brand": len([t for t in texts if t]),
            "sample_windows": [],
        }

    for text in texts:
        if not text:
            continue
        chunks: list[str] = []
        for t in terms:
            for m in re.finditer(re.escape(t), text, flags=re.IGNORECASE):
                s = max(0, m.start() - 40)
                e = min(len(text), m.end() + 40)
                chunks.append(text[s:e])
        if not chunks:
            # 未问到/未提到该品牌：不参与正负面打分
            skipped_no_brand += 1
            continue
        for ch in chunks:
            brand_mention_windows += 1
            windows.append(ch)
            for w in _POS:
                if w in ch:
                    pos_hits += 1
            for w in _NEG:
                if w in ch:
                    neg_hits += 1

    total = pos_hits + neg_hits
    if brand_mention_windows == 0:
        score = 50.0
        label = "中性 / 未提及该品牌"
    elif total == 0:
        score = 50.0
        label = "中性 / 信号不足"
    else:
        # 映射到 0–100：全负→约 15，全正→约 85，平衡→50
        ratio = pos_hits / total
        score = 15.0 + ratio * 70.0
        if score >= 62:
            label = "偏正面"
        elif score <= 38:
            label = "偏负面"
        else:
            label = "正负交织"
    return _clip(score), label, {
        "positive_hits": pos_hits,
        "negative_hits": neg_hits,
        "brand_mention_windows": brand_mention_windows,
        "skipped_no_brand": skipped_no_brand,
        "sample_windows": windows[:8],
    }


def _keyword_coverage(
    ask_logs: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    terms: list[str],
) -> tuple[float, dict[str, Any]]:
    ok_logs = [a for a in ask_logs if a.get("status") == "ok"]
    brand_in_answer = 0
    for a in ok_logs:
        if _mentions_brand(a.get("answer_text") or "", terms):
            brand_in_answer += 1
    answer_rate = (brand_in_answer / len(ok_logs)) if ok_logs else 0.0

    brand_in_cite = 0
    for c in citations:
        blob = " ".join(
            x for x in [c.get("segment") or "", c.get("title") or "", c.get("answer_excerpt") or ""] if x
        )
        if _mentions_brand(blob, terms):
            brand_in_cite += 1
    cite_rate = (brand_in_cite / len(citations)) if citations else 0.0

    # 提问词落入引用的覆盖
    rows = [
        {
            "weight": 1.0,
            "question_text": c.get("question_text") or "",
            "segment": c.get("segment") or c.get("title") or "",
            "title": c.get("title") or "",
        }
        for c in citations
    ]
    kw = analyze_keyword_signals(rows) if rows else {}
    overlap = kw.get("overlap_keywords") or []
    by_q: dict[str, list[dict]] = {}
    for c in citations:
        by_q.setdefault(c.get("question_text") or "", []).append(c)
    q_with_overlap = 0
    for qt, cs in by_q.items():
        q_toks = set(tokenize(qt))
        hit = False
        for c in cs:
            seg = c.get("segment") or c.get("title") or ""
            if q_toks & set(tokenize(seg)):
                hit = True
                break
        if hit:
            q_with_overlap += 1
    overlap_rate = (q_with_overlap / len(by_q)) if by_q else 0.0

    score = 100.0 * (0.4 * answer_rate + 0.3 * cite_rate + 0.3 * overlap_rate)
    detail = {
        "brand_in_answer_rate": round(answer_rate, 3),
        "brand_in_citation_rate": round(cite_rate, 3),
        "question_overlap_rate": round(overlap_rate, 3),
        "answered_ok": len(ok_logs),
        "citation_count": len(citations),
        "top_overlap_keywords": overlap[:10],
        "keyword_tips": (kw.get("keyword_tips") or [])[:5],
    }
    return _clip(score), detail


def _answer_priority(
    ask_logs: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    terms: list[str],
) -> tuple[float, dict[str, Any]]:
    """答案展示优先级：品牌/官网是否靠前出现、引用是否靠前。"""
    ok_logs = [a for a in ask_logs if a.get("status") == "ok" and (a.get("answer_text") or "").strip()]
    early_brand = 0
    for a in ok_logs:
        text = a.get("answer_text") or ""
        head = text[:220]
        if _mentions_brand(head, terms):
            early_brand += 1
    early_rate = (early_brand / len(ok_logs)) if ok_logs else 0.0

    # 按问题看前 3 条引用是否含官网/品牌
    by_q: dict[Any, list[dict]] = {}
    for c in citations:
        by_q.setdefault(c.get("question_id") or c.get("question_text"), []).append(c)

    official_early = 0
    brand_cite_early = 0
    q_n = 0
    for _qid, cs in by_q.items():
        q_n += 1
        top = cs[:3]  # 入库顺序 ≈ 答案中出现顺序
        if any(c.get("is_official") for c in top):
            official_early += 1
        if any(
            _mentions_brand(
                " ".join(x for x in [c.get("segment") or "", c.get("title") or ""] if x),
                terms,
            )
            for c in top
        ):
            brand_cite_early += 1

    off_rate = (official_early / q_n) if q_n else 0.0
    brand_cite_rate = (brand_cite_early / q_n) if q_n else 0.0
    cite_density = min(1.0, (len(citations) / max(1, len(ok_logs))) / 4.0)  # ~4 cites/q → 满分

    score = 100.0 * (0.35 * early_rate + 0.25 * off_rate + 0.25 * brand_cite_rate + 0.15 * cite_density)
    detail = {
        "early_brand_in_answer_rate": round(early_rate, 3),
        "official_in_top3_rate": round(off_rate, 3),
        "brand_cite_in_top3_rate": round(brand_cite_rate, 3),
        "avg_citations_per_answer": round(len(citations) / max(1, len(ok_logs)), 2),
    }
    return _clip(score), detail


def _build_advice(
    *,
    brand_name: str,
    keyword_score: float,
    priority_score: float,
    sentiment_score: float,
    sentiment_label: str,
    kw_detail: dict[str, Any],
    pri_detail: dict[str, Any],
    citations: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    bn = brand_name or "该品牌"

    lines.append(
        f"「{bn}」本轮检测：关键词覆盖广度 {keyword_score}、答案展示优先级 {priority_score}、"
        f"正负面 {sentiment_score}（{sentiment_label}）。"
    )

    if keyword_score < 45:
        lines.append(
            "关键词覆盖偏弱：答案与引用中品牌/问法对齐不足。建议在可摘取结论句中原样复用用户问法关键词，"
            "并明确写出品牌全称与常用别名。"
        )
    elif keyword_score < 70:
        lines.append(
            "关键词覆盖中等：可继续强化「提问→引用对齐词」，把高频对齐词放进段首结论。"
        )
    else:
        lines.append("关键词覆盖较好：保持问法对齐，并扩展长尾问法覆盖面。")

    tips = kw_detail.get("keyword_tips") or []
    if tips:
        lines.append(tips[0])

    if priority_score < 45:
        lines.append(
            "展示优先级偏低：品牌/官网信息出现偏后。建议在文首 1–2 句给出品牌定位结论，"
            "并建设可被优先引用的官网产品页与权威评测页。"
        )
    elif pri_detail.get("official_in_top3_rate", 0) < 0.2:
        lines.append(
            "官网进入答案前排引用较少：优先完善官网 FAQ/参数页的可摘取短句，提升权威信源权重。"
        )
    else:
        lines.append("答案前段已较多露出品牌或官网引用，可继续巩固权威信源占比。")

    # 站点与资料分级
    sites = Counter(
        ("官网" if c.get("is_official") else (c.get("site_name") or "未知")) for c in citations
    )
    levels = Counter((c.get("content_level") or "未分级") for c in citations)
    if sites:
        top_sites = "、".join(f"{s}×{n}" for s, n in sites.most_common(5))
        lines.append(f"本轮主要引用站点：{top_sites}。投放时可优先深耕这些高被引渠道。")
    if levels:
        top_lv = "、".join(f"{s}×{n}" for s, n in levels.most_common(3))
        lines.append(f"资料分级分布：{top_lv}。按主分级补齐对应体裁（评测/参数/口碑/教程）。")

    if sentiment_score <= 38:
        lines.append(
            "舆情偏负面：投放内容应主动回应痛点、给出可核验对比与售后保障信息，避免空泛好评。"
        )
    elif sentiment_score >= 62:
        lines.append(
            "舆情偏正面：可扩大场景化种草与对比结论类内容，固化「推荐/适合」类可摘句式。"
        )
    else:
        lines.append(
            "舆情中性或交织：用事实参数与场景结论引导，减少易被截成负面的模糊表述。"
        )

    lines.append(
        "综合投放建议：① 文首结论含品牌名+对齐词；② 官网与权威第三方各布局可摘取事实句；"
        "③ 按本轮主分级补齐体裁；④ 定期复测同一问题库观察三项分数变化。"
    )
    return "\n".join(lines)


def analyze_run_payload(
    *,
    brand_name: str,
    aliases: list[str] | None,
    ask_logs: list[dict[str, Any]],
    citations: list[dict[str, Any]],
) -> dict[str, Any]:
    terms = _brand_terms(brand_name, aliases)
    kw_score, kw_detail = _keyword_coverage(ask_logs, citations, terms)
    pri_score, pri_detail = _answer_priority(ask_logs, citations, terms)
    texts = [(a.get("answer_text") or "") for a in ask_logs if a.get("status") == "ok"]
    # 补充引用段落
    texts.extend((c.get("segment") or "") for c in citations if c.get("segment"))
    sent_score, sent_label, sent_detail = _sentiment_score(texts, terms)
    advice = _build_advice(
        brand_name=brand_name,
        keyword_score=kw_score,
        priority_score=pri_score,
        sentiment_score=sent_score,
        sentiment_label=sent_label,
        kw_detail=kw_detail,
        pri_detail=pri_detail,
        citations=citations,
    )
    return {
        "keyword_coverage": kw_score,
        "answer_priority": pri_score,
        "sentiment_score": sent_score,
        "sentiment_label": sent_label,
        "advice": advice,
        "details": {
            "keyword": kw_detail,
            "priority": pri_detail,
            "sentiment": sent_detail,
            "stats": {
                "ask_ok": sum(1 for a in ask_logs if a.get("status") == "ok"),
                "ask_error": sum(1 for a in ask_logs if a.get("status") == "error"),
                "citation_count": len(citations),
            },
        },
    }
