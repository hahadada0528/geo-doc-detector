"""投放网站引用分析：按时间衰减权重统计更易被 AI 采纳的写法与形式。"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app.article_types import (
    ARTICLE_TYPES,
    CITATION_FORMATS,
    aggregate_named_rank,
    detect_article_types,
    detect_citation_formats,
    recommend_article_plan,
)
from app.keywords import analyze_keyword_signals
from app.levels import aggregate_level_rank, classify_content_level

# 权重：1–30 天 1.0；31–60 天 0.8；61–90 天 0.65；91–180 天 0.45；181–365 天 0.2
# 边界取更高档（第 30 天算 1.0，第 60 天算 0.8）
WEIGHT_RULES: list[tuple[int, float]] = [
    (30, 1.0),
    (60, 0.8),
    (90, 0.65),
    (180, 0.45),
    (365, 0.2),
]

FORM_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("问答体", "以问句引出、再直接作答，贴近用户检索问法", re.compile(r"(问[:：]|答[:：]|Q[:：]|A[:：]|常见问题|FAQ)", re.I)),
    ("定义说明", "开篇给清晰定义或「是什么」式表述", re.compile(r"(是指|定义为|所谓|指的是|可以理解为|本质上是)")),
    ("步骤教程", "分步骤、方法论、操作指南", re.compile(r"(步骤|如何|怎么|教程|方法|流程|首先|然后|最后|第[一二三四五六七八九十\d]+[步点])")),
    ("数据论据", "带具体数字、占比、价格、参数", re.compile(r"(\d+(\.\d+)?\s*%|\d+(\.\d+)?\s*(万|亿|元|公里|小时|天|款|项)|约\s*\d+)")),
    ("对比评测", "对比、优劣、VS、横向评测", re.compile(r"(对比|相比|比起|优缺点|VS|vs|versus|哪个好|区别)", re.I)),
    ("榜单推荐", "排名、TOP、推荐清单", re.compile(r"(TOP\s*\d+|排行|排名|推荐榜|盘点|精选|清单)", re.I)),
    ("分点列举", "用 1.2.3. 或条目化拆解要点", re.compile(r"(^|\n)\s*([（(]?\d+[）).、]|[-•·*]|\d+、)")),
    ("场景结论", "面向使用场景给出明确结论/建议", re.compile(r"(适合|建议|推荐|总结|结论|一句话|总的来说|因此)")),
]


def parse_added_at(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def age_days(added_at: str | None, now: datetime | None = None) -> int | None:
    dt = parse_added_at(added_at)
    if not dt:
        return None
    now = now or datetime.now(timezone.utc)
    delta = now - dt
    return max(0, delta.days)


def time_weight(added_at: str | None, now: datetime | None = None) -> float:
    days = age_days(added_at, now)
    if days is None:
        return 0.0
    if days < 1:
        return 1.0  # 当天计入最高档
    for upper, weight in WEIGHT_RULES:
        if days <= upper:
            return weight
    return 0.0


def normalize_site_query(site: str) -> str:
    raw = (site or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw or raw.startswith("www."):
        try:
            host = urlparse(raw if "://" in raw else "https://" + raw).hostname or raw
            return host.removeprefix("www.")
        except Exception:
            return raw.removeprefix("www.")
    return raw


def detect_forms(text: str) -> list[str]:
    if not text or not text.strip():
        return []
    hit = []
    for key, _desc, pattern in FORM_PATTERNS:
        if pattern.search(text):
            hit.append(key)
    return hit


def _avg_len(segments: list[tuple[str, float]]) -> float:
    total_w = sum(w for _, w in segments)
    if total_w <= 0:
        return 0.0
    return sum(len(s) * w for s, w in segments) / total_w


def _sentence_stats(segments: list[tuple[str, float]]) -> dict[str, float]:
    """加权平均句长、问句占比。"""
    q_w = 0.0
    sent_len_acc = 0.0
    sent_w = 0.0
    for text, w in segments:
        parts = [p.strip() for p in re.split(r"[。！？!?]", text) if p.strip()]
        if not parts:
            continue
        for p in parts:
            sent_len_acc += len(p) * w
            sent_w += w
            if "？" in text or "?" in text or p.endswith("吗") or p.endswith("呢"):
                q_w += w
    return {
        "avg_sentence_len": (sent_len_acc / sent_w) if sent_w else 0.0,
        "question_weight_share": (q_w / sent_w) if sent_w else 0.0,
    }


def analyze_site_citations(
    site_query: str,
    citations: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """对某投放站的引用做时间加权分析，输出写法建议。"""
    now = now or datetime.now(timezone.utc)
    form_scores: dict[str, float] = defaultdict(float)
    form_counts: dict[str, int] = defaultdict(int)
    bucket_stats: dict[str, dict[str, float | int]] = {
        "1-30天(1.0)": {"count": 0, "weight": 0.0},
        "31-60天(0.8)": {"count": 0, "weight": 0.0},
        "61-90天(0.65)": {"count": 0, "weight": 0.0},
        "91-180天(0.45)": {"count": 0, "weight": 0.0},
        "181-365天(0.2)": {"count": 0, "weight": 0.0},
        "超一年(不计)": {"count": 0, "weight": 0.0},
    }
    weighted_segments: list[tuple[str, float]] = []
    samples: list[dict[str, Any]] = []
    level_rows: list[dict[str, Any]] = []
    article_type_rows: list[dict[str, Any]] = []
    citation_format_rows: list[dict[str, Any]] = []
    keyword_rows: list[dict[str, Any]] = []
    total_weight = 0.0
    used = 0
    skipped_old = 0

    for c in citations:
        w = time_weight(c.get("added_at"), now)
        days = age_days(c.get("added_at"), now)
        bucket = _bucket_label(days, w)
        bucket_stats[bucket]["count"] = int(bucket_stats[bucket]["count"]) + 1
        bucket_stats[bucket]["weight"] = float(bucket_stats[bucket]["weight"]) + w

        if w <= 0:
            skipped_old += 1
            continue

        used += 1
        total_weight += w
        text = " ".join(
            x for x in [c.get("title") or "", c.get("segment") or "", c.get("answer_excerpt") or ""] if x
        )
        forms = detect_forms(text)
        if not forms:
            forms = ["短句陈述"]  # 无明显结构时记为直述
            form_scores["短句陈述"] += w * 0.5  # 弱权重，避免淹没结构化写法
            form_counts["短句陈述"] += 1
        else:
            for f in forms:
                form_scores[f] += w
                form_counts[f] += 1

        level = classify_content_level(
            text,
            url=c.get("url"),
            site_name=c.get("site_name"),
            manual_level=c.get("content_level") or None,
        )
        level_rows.append({"level_id": level["level_id"], "weight": w})

        article_types = detect_article_types(
            title=c.get("title") or "",
            segment=c.get("segment") or "",
            url=c.get("url") or "",
            question=c.get("question_text") or "",
            site_name=c.get("site_name") or "",
        )
        cite_formats = detect_citation_formats(c.get("segment") or "", c.get("title") or "")
        article_type_rows.append({"article_types": article_types, "weight": w})
        citation_format_rows.append({"citation_formats": cite_formats, "weight": w})

        seg = (c.get("segment") or c.get("title") or "").strip()
        if seg:
            weighted_segments.append((seg, w))
        keyword_rows.append(
            {
                "weight": w,
                "question_text": c.get("question_text") or "",
                "segment": c.get("segment") or "",
                "title": c.get("title") or "",
            }
        )

        samples.append(
            {
                "weight": w,
                "age_days": days,
                "site_name": c.get("site_name"),
                "url": c.get("url"),
                "title": c.get("title"),
                "segment": c.get("segment"),
                "question_text": c.get("question_text"),
                "forms": forms,
                "level_name": level["level_name"],
                "level_id": level["level_id"],
                "article_types": article_types,
                "citation_formats": cite_formats,
                "added_at": c.get("added_at"),
            }
        )

    samples.sort(
        key=lambda x: (
            -float(x["weight"]),
            x["age_days"] if x.get("age_days") is not None else 10**9,
        )
    )
    form_rank = sorted(
        (
            {
                "form": name,
                "score": round(score, 3),
                "count": form_counts[name],
                "share": round(score / total_weight, 3) if total_weight else 0.0,
                "tip": next((d for k, d, _ in FORM_PATTERNS if k == name), "用简洁陈述句直接回答问题"),
            }
            for name, score in form_scores.items()
        ),
        key=lambda x: -x["score"],
    )
    level_rank = aggregate_level_rank(level_rows)
    article_type_rank = aggregate_named_rank(
        article_type_rows, key="article_types", catalog=ARTICLE_TYPES + [{"name": "综合资讯文", "write_tip": "综合资讯需拆成可摘取事实句。", "desc": ""}]
    )
    citation_format_rank = aggregate_named_rank(
        citation_format_rows,
        key="citation_formats",
        catalog=CITATION_FORMATS + [{"name": "普通陈述句", "write_tip": "改成结论先行或带数字的事实句。", "desc": ""}],
    )
    keyword_signals = analyze_keyword_signals(keyword_rows)

    stats = _sentence_stats(weighted_segments)
    avg_seg = _avg_len(weighted_segments)
    advice = _build_advice(form_rank, avg_seg, stats, site_query)
    if level_rank:
        top_lv = level_rank[0]["level_name"]
        mix = "、".join(f"{x['level_name']}({int(x['share']*100)}%)" for x in level_rank[:3])
        advice.insert(0, f"该站加权主资料分级为「{top_lv}」；建议配比：{mix}。")
    type_plan = recommend_article_plan(article_type_rank, citation_format_rank, site_query)
    # 关键词/句式建议 + 文章类型建议放最前
    advice = (keyword_signals.get("keyword_tips") or []) + type_plan + advice

    from app.writing_spec import build_writing_spec, writing_spec_to_markdown

    result = {
        "site_query": site_query,
        "matched": len(citations),
        "used": used,
        "skipped_old": skipped_old,
        "total_weight": round(total_weight, 3),
        "bucket_stats": bucket_stats,
        "form_rank": form_rank,
        "level_rank": level_rank,
        "article_type_rank": article_type_rank,
        "citation_format_rank": citation_format_rank,
        "recommended_article_type": article_type_rank[0]["name"] if article_type_rank else None,
        "recommended_citation_formats": [x["name"] for x in citation_format_rank[:3]],
        "primary_level": level_rank[0]["level_name"] if level_rank else None,
        "overlap_keywords": keyword_signals.get("overlap_keywords") or [],
        "cited_keywords": keyword_signals.get("cited_keywords") or [],
        "cited_phrases": keyword_signals.get("cited_phrases") or [],
        "sentence_patterns": keyword_signals.get("sentence_patterns") or [],
        "char_features": keyword_signals.get("char_features") or [],
        "keyword_tips": keyword_signals.get("keyword_tips") or [],
        "avg_segment_len": round(avg_seg, 1),
        "avg_sentence_len": round(stats["avg_sentence_len"], 1),
        "question_weight_share": round(stats["question_weight_share"], 3),
        "advice": advice,
        "samples": samples[:20],
        "weight_legend": [
            {"range": "1–30 天", "weight": 1.0},
            {"range": "31–60 天", "weight": 0.8},
            {"range": "61–90 天", "weight": 0.65},
            {"range": "91–180 天", "weight": 0.45},
            {"range": "181–365 天", "weight": 0.2},
            {"range": ">365 天", "weight": 0.0},
        ],
    }
    spec = build_writing_spec(result, site=site_query)
    result["writing_spec"] = spec
    result["writing_spec_md"] = writing_spec_to_markdown(spec)
    return result


def _bucket_label(days: int | None, weight: float) -> str:
    if days is None:
        return "超一年(不计)"
    if days <= 30:
        return "1-30天(1.0)"
    if days <= 60:
        return "31-60天(0.8)"
    if days <= 90:
        return "61-90天(0.65)"
    if days <= 180:
        return "91-180天(0.45)"
    if days <= 365:
        return "181-365天(0.2)"
    return "超一年(不计)"


def _build_advice(
    form_rank: list[dict[str, Any]],
    avg_seg: float,
    stats: dict[str, float],
    site_query: str,
) -> list[str]:
    tips: list[str] = []
    if not form_rank:
        tips.append(
            f'在「{site_query}」暂无有效加权引用，请先跑检测积累数据，或换站点名/域名再试。'
        )
        return tips

    top = form_rank[:3]
    tips.append(
        "该站近期更易被 AI 采纳的内容形式（按时间加权）："
        + "、".join(f"{x['form']}（权重占比 {int(x['share'] * 100)}%）" for x in top)
        + "。"
    )
    for item in top:
        tips.append(f"· {item['form']}：{item['tip']}")

    if avg_seg:
        if avg_seg < 40:
            tips.append(f"被引用段落偏短（加权均长约 {avg_seg:.0f} 字），宜写可独立成句的结论句，方便模型整段摘取。")
        elif avg_seg > 120:
            tips.append(f"被引用段落偏长（加权均长约 {avg_seg:.0f} 字），建议段首先给结论，再展开论据，便于 AI 截取前段。")
        else:
            tips.append(f"被引用段落加权均长约 {avg_seg:.0f} 字，保持「一段一观点」较稳妥。")

    if stats["avg_sentence_len"]:
        tips.append(f"句子加权均长约 {stats['avg_sentence_len']:.0f} 字；避免空话，优先可核验的事实与参数。")

    # 写作结构建议
    preferred = {x["form"] for x in top}
    structure = []
    if "定义说明" in preferred:
        structure.append("首段用 1–2 句给出定义/定位")
    if "数据论据" in preferred:
        structure.append("紧跟可核验数字（参数、占比、价格区间）")
    if "分点列举" in preferred or "步骤教程" in preferred:
        structure.append("主体用 3–5 个编号要点展开")
    if "对比评测" in preferred:
        structure.append("用对比句说清差异，再给选择建议")
    if "场景结论" in preferred:
        structure.append("结尾给出明确适用场景与一句话结论")
    if not structure:
        structure = ["标题贴合用户原问题措辞", "正文先答后展", "段内出现可摘取的完整事实句"]
    tips.append("建议成稿结构：" + " → ".join(structure) + "。")
    tips.append("段首结论句优先复用「提问→引用对齐词」，并带数字或断言句式，提高被摘取概率。")
    return tips
