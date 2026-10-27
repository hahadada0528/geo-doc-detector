"""提问-引用关键词关系：哪些词/句式/字符特征更容易被 AI 引用。"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

# 无信息停用词（过短、虚词、通用连接）
_STOP = {
    "的", "了", "是", "在", "和", "与", "及", "或", "也", "都", "就", "都", "而", "被", "把",
    "让", "给", "从", "对", "向", "以", "为", "有", "没", "不", "很", "最", "更", "还",
    "这", "那", "这个", "那个", "一个", "一些", "一种", "可以", "可能", "如果", "因为",
    "所以", "但是", "然后", "以及", "进行", "通过", "对于", "关于", "作为", "已经",
    "什么", "怎么", "如何", "哪些", "哪个", "多少", "是否", "能否", "怎样",
    "我们", "你们", "他们", "自己", "大家", "没有", "不是", "就是", "还是", "或者",
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "with",
}

# 易引用句式标记（出现在被引段落里的连接/断言词）
_PATTERN_MARKERS: list[tuple[str, str, re.Pattern[str]]] = [
    ("断言结论", "用「建议/适合/因此/结论是」直接下判断", re.compile(r"(建议|适合|因此|所以|结论是|综上|总之|一句话)")),
    ("定义解释", "用「是指/定义为/所谓」做知识卡片句", re.compile(r"(是指|定义为|所谓|指的是|可以理解为)")),
    ("对比转折", "用「相比/比…更/不如/但」突出差异", re.compile(r"(相比|比起|对比|不如|优于|但是|然而|却|更)")),
    ("数量事实", "嵌入具体数字/单位，便于核验摘取", re.compile(r"\d+(\.\d+)?\s*(%|万|亿|元|公里|小时|天|款|项|km|kWh|GB|英寸)?")),
    ("程度限定", "用「约/左右/左右/最高/最低」限定表述", re.compile(r"(约|左右|将近|最高|最低|最多|至少|超过|不足)")),
    ("问答触发", "段内保留问法痕迹（吗/呢/？）再给短答", re.compile(r"[？?]|吗|呢|如何|怎么|多少")),
    ("列举衔接", "用「首先/其次/此外/另外」做条目前置词", re.compile(r"(首先|其次|再次|此外|另外|最后|其一|其二)")),
    ("引述权威", "用「官方/据…/其表示」提高可引用感", re.compile(r"(官方|据|表示|指出|显示|数据显示)")),
]

_CHAR_FEATURES: list[tuple[str, str, Any]] = [
    ("含阿拉伯数字", "段内放可核验数字", lambda t: bool(re.search(r"\d", t))),
    ("含百分号%", "适合写占比/增长类结论", lambda t: "%" in t or "％" in t),
    ("含书名号/引号", "关键短句用「」或“”标出", lambda t: bool(re.search(r"[「」『』“”\"]", t))),
    ("含顿号、", "并列要点用顿号压缩成一句", lambda t: "、" in t),
    ("含冒号：", "用「结论：…」或「答：…」结构", lambda t: "：" in t or ":" in t),
    ("含破折/连接-", "适合写区间与型号（如 19.99-21.59）", lambda t: bool(re.search(r"[-—–~～]", t))),
    ("句末句号。", "写成完整句，避免半截短语", lambda t: "。" in t or "." in t),
    ("含问号？", "小标题/段首保留用户问法", lambda t: "？" in t or "?" in t),
]


def tokenize(text: str) -> list[str]:
    """中英混合粗分词：英文词 + 中文 2/3 字窗。"""
    text = (text or "").lower()
    tokens: list[str] = []
    for m in re.finditer(r"[a-z][a-z0-9\-]{1,}|[\u4e00-\u9fff]{2,}", text):
        w = m.group(0)
        if re.fullmatch(r"[a-z][a-z0-9\-]{1,}", w):
            if w not in _STOP and len(w) >= 2:
                tokens.append(w)
            continue
        # 中文：整段切 2-gram，并保留 3-gram
        chars = w
        if len(chars) == 2 and chars not in _STOP:
            tokens.append(chars)
        elif len(chars) >= 3:
            for i in range(len(chars) - 1):
                bi = chars[i : i + 2]
                if bi not in _STOP:
                    tokens.append(bi)
            for i in range(len(chars) - 2):
                tri = chars[i : i + 3]
                if tri not in _STOP:
                    tokens.append(tri)
    return tokens


def _rank_counter(counter: dict[str, float], *, limit: int = 20) -> list[dict[str, Any]]:
    total = sum(counter.values()) or 1.0
    ranked = sorted(counter.items(), key=lambda x: (-x[1], x[0]))[:limit]
    return [
        {"term": k, "score": round(v, 3), "share": round(v / total, 3)}
        for k, v in ranked
    ]


def analyze_keyword_signals(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """rows: [{weight, question_text, segment, title}, ...] 已按时间加权可用样本。"""
    cite_terms: dict[str, float] = defaultdict(float)
    question_terms: dict[str, float] = defaultdict(float)
    overlap_terms: dict[str, float] = defaultdict(float)  # 问句词出现在引用里
    bigrams: dict[str, float] = defaultdict(float)
    pattern_scores: dict[str, float] = defaultdict(float)
    pattern_tips: dict[str, str] = {}
    char_scores: dict[str, float] = defaultdict(float)
    char_tips: dict[str, str] = {}
    total_w = 0.0

    for r in rows:
        w = float(r.get("weight") or 0)
        if w <= 0:
            continue
        total_w += w
        q = r.get("question_text") or ""
        seg = (r.get("segment") or r.get("title") or "").strip()
        if not seg:
            continue

        q_toks = set(tokenize(q))
        c_toks = tokenize(seg)
        c_set = set(c_toks)

        for t in q_toks:
            question_terms[t] += w
        for t in c_toks:
            cite_terms[t] += w
        for t in q_toks & c_set:
            overlap_terms[t] += w

        # 引用内相邻二元组（用已切 token 序列）
        for i in range(len(c_toks) - 1):
            a, b = c_toks[i], c_toks[i + 1]
            if a == b:
                continue
            bigrams[f"{a}{b}" if re.search(r"[\u4e00-\u9fff]", a + b) else f"{a} {b}"] += w

        for name, tip, pat in _PATTERN_MARKERS:
            if pat.search(seg):
                pattern_scores[name] += w
                pattern_tips[name] = tip

        for name, tip, fn in _CHAR_FEATURES:
            if fn(seg):
                char_scores[name] += w
                char_tips[name] = tip

    cite_rank = _rank_counter(cite_terms, limit=25)
    overlap_rank = _rank_counter(overlap_terms, limit=20)
    question_rank = _rank_counter(question_terms, limit=15)
    phrase_rank = _rank_counter(bigrams, limit=20)

    pattern_rank = [
        {
            "name": k,
            "score": round(v, 3),
            "share": round(v / total_w, 3) if total_w else 0.0,
            "tip": pattern_tips.get(k, ""),
        }
        for k, v in sorted(pattern_scores.items(), key=lambda x: -x[1])
    ]
    char_rank = [
        {
            "name": k,
            "score": round(v, 3),
            "share": round(v / total_w, 3) if total_w else 0.0,
            "tip": char_tips.get(k, ""),
        }
        for k, v in sorted(char_scores.items(), key=lambda x: -x[1])
    ]

    tips = _build_keyword_tips(overlap_rank, cite_rank, phrase_rank, pattern_rank, char_rank)
    return {
        "overlap_keywords": overlap_rank,  # 提问词落入引用：对齐检索问法的关键
        "cited_keywords": cite_rank,  # 引用段落高频词
        "question_keywords": question_rank,
        "cited_phrases": phrase_rank,
        "sentence_patterns": pattern_rank,
        "char_features": char_rank,
        "keyword_tips": tips,
    }


def _build_keyword_tips(
    overlap: list[dict[str, Any]],
    cited: list[dict[str, Any]],
    phrases: list[dict[str, Any]],
    patterns: list[dict[str, Any]],
    chars: list[dict[str, Any]],
) -> list[str]:
    tips: list[str] = []
    if overlap:
        words = "、".join(x["term"] for x in overlap[:8])
        tips.append(
            f"提问→引用对齐词（问句里的词也出现在被引段落）：{words}。"
            "写稿时把这些词原样放进可摘取结论句，提高与用户问法的咬合。"
        )
    if cited:
        words = "、".join(x["term"] for x in cited[:10])
        tips.append(f"被引段落高频词：{words}。主体段落优先覆盖这些词，少用空泛形容词。")
    if phrases:
        ph = "、".join(x["term"] for x in phrases[:6])
        tips.append(f"高频搭配/短句块：{ph}。可直接作为小标题或段首结论的骨架。")
    if patterns:
        top = patterns[:3]
        tips.append(
            "更容易被引用的句式："
            + "；".join(f"{x['name']}（{int(x['share']*100)}%）— {x['tip']}" for x in top)
        )
    if chars:
        top = chars[:4]
        tips.append(
            "字符/标点特征："
            + "；".join(f"{x['name']}（{int(x['share']*100)}% 样本）— {x['tip']}" for x in top)
        )
    if not tips:
        tips.append("样本不足，暂无法统计关键词与句式；请先积累更多该站引用。")
    else:
        tips.append(
            "成稿技巧：先用「对齐词」回应问法 → 用「断言/数字句式」写 1 句可摘结论 → "
            "再用高频词展开论据，避免代词悬空（这/其/上述）。"
        )
    return tips
