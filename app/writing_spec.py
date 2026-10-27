"""把站点统计画像蒸馏成可直接写稿的「成稿规格书」。"""
from __future__ import annotations

import json
from typing import Any


def _names(rows: list[dict[str, Any]] | None, key: str = "name", n: int = 8) -> list[str]:
    out: list[str] = []
    for r in rows or []:
        v = r.get(key) or r.get("form") or r.get("term") or r.get("level_name")
        if v and str(v) not in out:
            out.append(str(v))
        if len(out) >= n:
            break
    return out


def _outline_for_type(article_type: str, primary_level: str) -> list[str]:
    """按推荐体裁给固定大纲，方便直接成稿。"""
    t = (article_type or "").strip()
    lv = (primary_level or "").strip()

    if "评测" in t or "对比" in t or "评测" in lv:
        return [
            "开篇：一句话结论（含品牌/品类 + 适用场景，事实导向）",
            "第2段：用 2–4 个可核验维度写清产品优势（体验/参数/价格/场景）",
            "第3段：数字或参数佐证（约/左右）；需要时可选一带中性对照",
            "第4段：更适合谁 / 需注意什么",
            "结尾：局限与适用边界（各一句，非安利）",
        ]
    if "教程" in t or "攻略" in t or "教程" in lv or "步骤" in t:
        return [
            "开篇：目标是什么 + 适合谁",
            "正文：分 3–5 步，每步以动词开头、可独立摘取",
            "穿插：关键参数或注意点（一句一事实）",
            "结尾：常见踩坑与适用边界",
        ]
    if "榜单" in t or "推荐" in t or "榜单" in lv:
        return [
            "开篇：榜单主题 + 评选维度一句话",
            "正文：名次 + 一句话理由 + 适合谁（每条可独立引用）",
            "结尾：如何按场景自选，避免唯一安利",
        ]
    if "百科" in t or "定义" in t or "百科" in lv:
        return [
            "开篇：「是指/定义为」清晰定义",
            "第2段：边界条件与常见误解",
            "第3段：与相近概念的区别（1–2 句）",
            "结尾：查阅/选购时关注的核验点",
        ]
    if "反馈" in t or "口碑" in t or "用户" in lv:
        return [
            "开篇：场景化体验结论（谁在什么情况下）",
            "正文：具体感受 + 对比感受（避免空泛好评）",
            "补充：痛点与可接受的取舍",
            "结尾：适用边界",
        ]
    if "产品" in t or "参数" in t or "产品信息" in lv:
        return [
            "开篇：产品定位一句（品类 + 核心卖点事实）",
            "正文：参数/规格分点，一句一事实",
            "补充：官网或权威来源可核验表述",
            "结尾：适用场景与需注意项",
        ]
    return [
        "开篇：定义或结论摘要（含对齐词，可独立摘取）",
        "第2–3段：按主资料分级展开 2–3 个要点，用事实突出产品优势",
        "第4段：适用场景与需注意项（可核验，非推销）",
        "结尾：局限与边界收束（非广告收束）",
    ]


def _donts() -> list[str]:
    return [
        "禁止输出广告文、软文、种草安利文、品牌通稿",
        "不要写促销口号、限时优惠、立即下单、加购引导、号召行动",
        "不要堆叠无依据的最高级（最好/第一/碾压/吊打/闭眼入）",
        "不要品牌通稿腔或反复安利；不要代言口吻",
        "不要关键词堆砌；一段一观点",
        "不要只喊优势口号：优势须落到可核验事实与适用边界",
    ]


def build_writing_spec(
    analysis: dict[str, Any] | None,
    *,
    site: str = "",
    topic: str = "",
    brand: str = "",
    word_count: int = 1200,
    notes: str = "",
) -> dict[str, Any]:
    """从 analyze / site_profile 蒸馏成成稿规格（结构化）。"""
    a = analysis or {}
    wc = max(400, min(int(word_count or 1200), 4000))

    article_type = (
        a.get("recommended_article_type")
        or (_names(a.get("article_type_rank"), "name", 1) or [""])[0]
        or "资料综述"
    )
    primary_level = (
        a.get("primary_level")
        or (_names(a.get("level_rank"), "level_name", 1) or ["综合信息级"])[0]
    )
    cite_formats = list(a.get("recommended_citation_formats") or []) or _names(
        a.get("citation_format_rank"), "name", 3
    )
    forms = _names(a.get("form_rank"), "form", 4)
    align_words = _names(a.get("overlap_keywords"), "term", 12)
    if not align_words:
        align_words = _names(a.get("cited_keywords"), "term", 12)
    phrases = _names(a.get("cited_phrases"), "term", 6)
    patterns = []
    for p in (a.get("sentence_patterns") or [])[:4]:
        name = p.get("name")
        tip = p.get("tip") or ""
        if name:
            patterns.append(f"{name}" + (f"：{tip}" if tip else ""))

    exemplars: list[str] = []
    sample_rows = a.get("samples") or a.get("top_examples") or []
    for s in sample_rows:
        seg = (s.get("segment") or s.get("title") or "").strip()
        if not seg:
            continue
        cut = seg
        for sep in ("。", "！", "？", "\n"):
            if sep in seg:
                cut = seg.split(sep)[0] + ("。" if sep == "。" else "")
                break
        cut = cut[:100].strip()
        if cut and cut not in exemplars:
            exemplars.append(cut)
        if len(exemplars) >= 3:
            break

    level_mix = []
    for lv in (a.get("level_rank") or [])[:3]:
        name = lv.get("level_name") or ""
        share = lv.get("share")
        if not name:
            continue
        if share is not None:
            level_mix.append(f"{name}（约 {int(float(share) * 100)}%）")
        else:
            level_mix.append(name)

    outline = _outline_for_type(str(article_type), str(primary_level))
    must_use = align_words[:8]
    style_notes = []
    if forms:
        style_notes.append("优先内容形式：" + "、".join(forms[:3]))
    if cite_formats:
        style_notes.append("优先引用格式：" + "、".join(cite_formats[:3]))
    if patterns:
        style_notes.append("优先句式：" + "；".join(patterns[:3]))
    avg_seg = a.get("avg_segment_len")
    if avg_seg:
        try:
            style_notes.append(f"参考段落长度约 {int(float(avg_seg))} 字")
        except (TypeError, ValueError):
            pass

    advice = [str(x) for x in (a.get("advice") or [])[:4] if x]
    kw_tips = [str(x) for x in (a.get("keyword_tips") or [])[:3] if x]

    spec: dict[str, Any] = {
        "成稿目标": "资料型非广告文稿，便于 AI 摘取引用；禁止广告文",
        "写作禁令": "禁止输出广告文/软文/种草安利/促销号召",
        "投放站点": (site or a.get("site") or "").strip() or None,
        "主题": (topic or "").strip() or None,
        "品牌": (brand or "").strip() or None,
        "目标字数": wc,
        "一文一型": article_type,
        "主资料分级": primary_level,
        "资料配比": level_mix,
        "结构大纲": outline,
        "段首必须对齐词": must_use,
        "可用搭配": phrases,
        "范例句式": exemplars,
        "写法要点": style_notes + advice + kw_tips,
        "明确不要": _donts(),
        "补充要求": (notes or "").strip() or None,
    }
    return {k: v for k, v in spec.items() if v not in (None, "", [], {})}


def writing_spec_to_markdown(spec: dict[str, Any]) -> str:
    """给人看的成稿规格书。"""
    lines: list[str] = ["# 成稿规格书", ""]
    meta_keys = ("成稿目标", "投放站点", "主题", "品牌", "目标字数", "一文一型", "主资料分级")
    for k in meta_keys:
        if k in spec:
            lines.append(f"- **{k}**：{spec[k]}")
    if spec.get("资料配比"):
        lines.append("- **资料配比**：" + "、".join(spec["资料配比"]))
    lines.append("")

    if spec.get("结构大纲"):
        lines.append("## 结构大纲")
        for i, step in enumerate(spec["结构大纲"], 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    if spec.get("段首必须对齐词"):
        lines.append("## 段首必须对齐词")
        lines.append("、".join(spec["段首必须对齐词"]))
        lines.append("")

    if spec.get("可用搭配"):
        lines.append("## 可用搭配")
        lines.append("、".join(spec["可用搭配"]))
        lines.append("")

    if spec.get("范例句式"):
        lines.append("## 范例句式（可改写后使用）")
        for ex in spec["范例句式"]:
            lines.append(f"- {ex}")
        lines.append("")

    if spec.get("写法要点"):
        lines.append("## 写法要点")
        for tip in spec["写法要点"]:
            lines.append(f"- {tip}")
        lines.append("")

    if spec.get("明确不要"):
        lines.append("## 明确不要")
        for x in spec["明确不要"]:
            lines.append(f"- {x}")
        lines.append("")

    if spec.get("补充要求"):
        lines.append("## 补充要求")
        lines.append(str(spec["补充要求"]))
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def writing_spec_to_brief_json(spec: dict[str, Any]) -> str:
    """发给写稿服务的紧凑 JSON。"""
    return json.dumps(spec, ensure_ascii=False, indent=2)
