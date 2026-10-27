"""本地 GEO Agent：加载最新训练包，对投放文章做加权匹配评估。"""
from __future__ import annotations

import re
from typing import Any

from app.agent_train import load_pack
from app.analyze import FORM_PATTERNS, detect_forms, normalize_site_query
from app.article_types import detect_article_types, detect_citation_formats
from app.levels import classify_content_level, detect_content_levels, level_tip


def _tokens(text: str) -> set[str]:
    text = (text or "").lower()
    return set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{2,}", text))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _resolve_profile(profiles: dict[str, Any], site: str) -> tuple[str, dict[str, Any] | None]:
    raw = (site or "").strip()
    if not raw:
        return "", None
    if raw in profiles:
        return raw, profiles[raw]

    q = normalize_site_query(raw) or raw.lower()
    for name, profile in profiles.items():
        n = name.lower()
        if q == n or q in n or n in q:
            return name, profile
        url_hint = q.replace("www.", "")
        if url_hint and url_hint in n:
            return name, profile

    if raw in ("官网", "官方网站") and "官网" in profiles:
        return "官网", profiles["官网"]
    return raw, None


def analyze_article_with_agent(
    *,
    site: str,
    article: str,
    pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pack = pack or load_pack()
    if not pack:
        return {
            "ok": False,
            "message": "还没有训练包。请先在「本地 Agent」里执行本月训练。",
        }

    article = (article or "").strip()
    if not article:
        return {"ok": False, "message": "请粘贴要分析的文章内容。"}

    profiles = pack.get("profiles") or {}
    examples = pack.get("examples") or []
    site_key, profile = _resolve_profile(profiles, site)

    article_forms = detect_forms(article) or ["短句陈述"]
    article_levels = detect_content_levels(article, site_name=site_key or site)
    primary_level = classify_content_level(article, site_name=site_key or site)
    article_types = detect_article_types(segment=article, site_name=site_key or site)
    citation_formats = detect_citation_formats(article)
    article_tokens = _tokens(article)

    form_rank = (profile or {}).get("form_rank") or []
    preferred = [f["form"] for f in form_rank[:5]]
    preferred_scores = {f["form"]: float(f.get("share") or 0) for f in form_rank}

    hit_forms = [f for f in article_forms if f in preferred_scores]
    miss_forms = [f for f in preferred if f not in article_forms]

    form_score = 0.0
    if preferred_scores:
        form_score = sum(preferred_scores.get(f, 0) for f in hit_forms)
        denom = sum(float(f.get("share") or 0) for f in form_rank[:3]) or 1.0
        form_score = min(1.0, form_score / denom)
    else:
        form_score = min(1.0, len([f for f in article_forms if f != "短句陈述"]) * 0.25)

    # 资料分级匹配
    level_rank = (profile or {}).get("level_rank") or []
    preferred_levels = [x["level_name"] for x in level_rank[:5]]
    level_share = {x["level_name"]: float(x.get("share") or 0) for x in level_rank}
    hit_levels = [n for n in article_levels if n in level_share]
    miss_levels = [n for n in preferred_levels if n not in article_levels]

    level_score = 0.0
    if level_share:
        level_score = sum(level_share.get(n, 0) for n in hit_levels)
        denom_l = sum(float(x.get("share") or 0) for x in level_rank[:3]) or 1.0
        level_score = min(1.0, level_score / denom_l)
        if primary_level["level_name"] == (profile or {}).get("primary_level"):
            level_score = min(1.0, level_score + 0.15)
    else:
        level_score = 0.35 if primary_level["level_id"] != "general" else 0.15

    # 文章类型 & 引用格式匹配
    type_rank = (profile or {}).get("article_type_rank") or []
    type_share = {x["name"]: float(x.get("share") or 0) for x in type_rank}
    preferred_types = [x["name"] for x in type_rank[:5]]
    hit_types = [n for n in article_types if n in type_share]
    miss_types = [n for n in preferred_types if n not in article_types]
    type_score = 0.0
    if type_share:
        type_score = sum(type_share.get(n, 0) for n in hit_types)
        denom_t = sum(float(x.get("share") or 0) for x in type_rank[:3]) or 1.0
        type_score = min(1.0, type_score / denom_t)
    else:
        type_score = 0.3 if article_types and article_types[0] != "综合资讯文" else 0.15

    fmt_rank = (profile or {}).get("citation_format_rank") or []
    fmt_share = {x["name"]: float(x.get("share") or 0) for x in fmt_rank}
    preferred_fmts = [x["name"] for x in fmt_rank[:5]]
    hit_fmts = [n for n in citation_formats if n in fmt_share]
    miss_fmts = [n for n in preferred_fmts if n not in citation_formats]
    fmt_score = 0.0
    if fmt_share:
        fmt_score = sum(fmt_share.get(n, 0) for n in hit_fmts)
        denom_f = sum(float(x.get("share") or 0) for x in fmt_rank[:3]) or 1.0
        fmt_score = min(1.0, fmt_score / denom_f)
    else:
        fmt_score = min(1.0, 0.25 * len([x for x in citation_formats if x != "普通陈述句"]))

    site_examples = [
        e
        for e in examples
        if (e.get("site_name") or "") == site_key
        or (site_key and site_key.lower() in (e.get("site_name") or "").lower())
    ]
    if not site_examples and profile is None:
        site_examples = examples

    sim_acc = 0.0
    w_acc = 0.0
    nearest: list[dict[str, Any]] = []
    for ex in site_examples:
        ex_tokens = set(ex.get("tokens") or _tokens(ex.get("segment") or ex.get("title") or ""))
        sim = _jaccard(article_tokens, ex_tokens)
        # 同分级样本额外加权
        same_level = 1.15 if ex.get("level_id") == primary_level["level_id"] else 1.0
        w = float(ex.get("weight") or 0) * same_level
        if w <= 0:
            continue
        sim_acc += sim * w
        w_acc += w
        nearest.append(
            {
                "similarity": round(sim, 3),
                "weight": float(ex.get("weight") or 0),
                "segment": (ex.get("segment") or ex.get("title") or "")[:200],
                "question_text": ex.get("question_text"),
                "forms": ex.get("forms") or [],
                "level_name": ex.get("level_name") or "",
            }
        )
    nearest.sort(key=lambda x: (-x["similarity"] * x["weight"], -x["weight"]))
    text_score = (sim_acc / w_acc) if w_acc else 0.0

    structure_hits = []
    first_block = article[:120]
    if re.search(r"(是指|定义为|所谓|结论|一句话|总结)", first_block):
        structure_hits.append("开篇有定义/结论")
    if re.search(r"(^|\n)\s*([（(]?\d+[）).、]|[-•·*])", article):
        structure_hits.append("含分点结构")
    if re.search(r"\d+(\.\d+)?\s*%|\d+\s*(万|亿|元)", article):
        structure_hits.append("含可核验数字")
    if re.search(r"(适合|建议|推荐|我觉得|入手后|用户反馈)", article):
        structure_hits.append("含场景/反馈表达")
    structure_score = min(1.0, len(structure_hits) / 3)

    # 综合分：分级20% + 文章类型20% + 引用格式20% + 形式15% + 样本相似15% + 结构10%
    overall = 100 * (
        0.20 * level_score
        + 0.20 * type_score
        + 0.20 * fmt_score
        + 0.15 * form_score
        + 0.15 * text_score
        + 0.10 * structure_score
    )

    tips: list[str] = []
    if profile:
        tips.append(
            f"已加载训练月 {pack['month_id']}「{site_key}」画像；主资料分级：{profile.get('primary_level') or '—'}；"
            f"推荐文章类型：{profile.get('recommended_article_type') or '—'}。"
        )
    else:
        tips.append(f"训练包中未找到「{site}」专属画像，已用全局样本弱评估。")

    tips.append(f"本文主分级判定为「{primary_level['level_name']}」。")
    tips.append(f"本文文章类型：{'、'.join(article_types)}；引用格式：{'、'.join(citation_formats) or '—'}。")
    if hit_types:
        tips.append("已对齐该站易被引文章类型：" + "、".join(hit_types))
    if miss_types:
        tips.append("建议改写成更接近：" + "、".join(miss_types[:2]))
        for item in type_rank:
            if item["name"] in miss_types[:2]:
                tips.append(f"· {item['name']}：{item.get('tip')}")
    if hit_fmts:
        tips.append("已命中易引用格式：" + "、".join(hit_fmts))
    if miss_fmts:
        tips.append("建议补齐引用友好格式：" + "、".join(miss_fmts[:3]))
        for item in fmt_rank:
            if item["name"] in miss_fmts[:3]:
                tips.append(f"· {item['name']}：{item.get('tip')}")
    if hit_levels:
        tips.append("已覆盖该站加权资料分级：" + "、".join(hit_levels))
    if miss_levels:
        tips.append("建议补齐资料分级：" + "、".join(miss_levels[:3]))
        for name in miss_levels[:3]:
            # 找 tip
            for item in level_rank:
                if item["level_name"] == name:
                    tips.append(f"· {name}：{item.get('tip') or level_tip(item.get('level_id') or 'general')}")
                    break

    if hit_forms:
        tips.append("已命中优先写法形式：" + "、".join(hit_forms))
    if miss_forms:
        tips.append("建议补齐写法形式：" + "、".join(miss_forms[:3]))
        for name in miss_forms[:3]:
            tip = next((d for k, d, _ in FORM_PATTERNS if k == name), "")
            if tip:
                tips.append(f"· {name}：{tip}")

    for adv in (profile or {}).get("advice") or []:
        if adv not in tips:
            tips.append(adv)
            if len(tips) >= 10:
                break

    if overall < 55:
        tips.append("匹配偏弱：先把主资料分级改到该站最高权重那一档，再补形式与高频问法。")
    elif overall < 75:
        tips.append("中等匹配：在现有分级上混入该站第二权重分级段落，并强化可摘取事实句。")
    else:
        tips.append("匹配较好：核对各分级段落是否都有独立可摘取的结论句。")

    return {
        "ok": True,
        "month_id": pack["month_id"],
        "site_query": site,
        "matched_site": site_key,
        "has_profile": profile is not None,
        "score": round(overall, 1),
        "subscores": {
            "level": round(level_score * 100, 1),
            "article_type": round(type_score * 100, 1),
            "citation_format": round(fmt_score * 100, 1),
            "form": round(form_score * 100, 1),
            "text_similarity": round(text_score * 100, 1),
            "structure": round(structure_score * 100, 1),
        },
        "article_forms": article_forms,
        "article_levels": article_levels,
        "article_types": article_types,
        "citation_formats": citation_formats,
        "primary_level": primary_level["level_name"],
        "preferred_forms": preferred,
        "preferred_levels": preferred_levels,
        "preferred_article_types": preferred_types,
        "preferred_citation_formats": preferred_fmts,
        "level_rank": level_rank,
        "article_type_rank": type_rank,
        "citation_format_rank": fmt_rank,
        "structure_hits": structure_hits,
        "tips": tips,
        "nearest_examples": nearest[:8],
        "manifest": pack.get("manifest"),
    }
