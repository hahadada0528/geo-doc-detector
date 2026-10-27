"""文章类型 & 易被引用格式：分析哪些类型/格式更容易被 AI 摘取。"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

# 适合写什么类型的文章（体裁）
ARTICLE_TYPES: list[dict[str, Any]] = [
    {
        "id": "review_compare",
        "name": "测评横评文",
        "desc": "多产品/方案对比、优缺点评测",
        "write_tip": "标题带「对比/测评」；开篇给结论，主体按维度横评，结尾写适合人群。",
        "patterns": [
            r"(评测|横评|对比评测|深度体验|上手体验|实测)",
            r"(优缺点|哪个好|VS|vs|对比|相比)",
            r"(review|comparison)",
        ],
        "url_hints": [r"/review", r"/pk", r"对比", r"评测"],
    },
    {
        "id": "buying_guide",
        "name": "选购攻略文",
        "desc": "怎么选、购买建议、避坑指南",
        "write_tip": "用「如何选/避坑」结构：需求分层 → 推荐档位 → 明确不适合谁。",
        "patterns": [
            r"(选购|怎么选|如何选|购买指南|避坑|攻略|指南)",
            r"(适合什么人|预算|档位推荐)",
        ],
        "url_hints": [r"选购", r"攻略", r"guide", r"how-to"],
    },
    {
        "id": "spec_explain",
        "name": "参数解读文",
        "desc": "规格、配置、价格、性能参数解读",
        "write_tip": "用参数表 + 一句话解释「这意味着什么」；关键数字单独成句便于摘取。",
        "patterns": [
            r"(参数|配置|规格|售价|定价|续航|芯片|处理器)",
            r"\d+(\.\d+)?\s*(万元|元|km|kWh|GB|英寸|马力)",
        ],
        "url_hints": [r"参数", r"配置", r"specs?", r"price"],
    },
    {
        "id": "user_story",
        "name": "用户体验文",
        "desc": "真实使用反馈、长期体验、口碑故事",
        "write_tip": "用具体场景叙事 + 可核对感受（天数/里程/频次），少空泛形容词。",
        "patterns": [
            r"(用户反馈|入手后|用了|体验下来|车主|买家|口碑|吐槽|真香)",
            r"(实际使用|日常通勤|开了.?天)",
        ],
        "url_hints": [r"体验", r"评测日记", r"日记", r"zhihu", r"xiaohongshu"],
    },
    {
        "id": "faq_article",
        "name": "FAQ 问答文",
        "desc": "常见问题集中解答",
        "write_tip": "每个小标题就是用户原问法；答句前 1–2 句必须可独立摘取。",
        "patterns": [
            r"(常见问题|FAQ|问[:：]|答[:：]|Q[:：]|A[:：])",
            r"(有人问|很多人关心|是否|能不能|可不可以)",
        ],
        "url_hints": [r"faq", r"问答", r"常见问题"],
    },
    {
        "id": "news_launch",
        "name": "新闻资讯文",
        "desc": "发布、上市、更新动态",
        "write_tip": "写清时间+事件+关键变化；少评价，多事实句。",
        "patterns": [
            r"(发布|上市|官宣|亮相|新品|更新|改款)",
            r"(发布会|消息称|据悉|报道)",
        ],
        "url_hints": [r"news", r"资讯", r"发布"],
    },
    {
        "id": "listicle",
        "name": "榜单盘点文",
        "desc": "TOP榜、精选清单、年度盘点",
        "write_tip": "名次 + 一句话理由 + 适合谁；理由里放可核验卖点。",
        "patterns": [
            r"(TOP\s*\d+|排行|排名|推荐榜|盘点|榜单|精选|清单)",
            r"(第一名|入选|热门推荐)",
        ],
        "url_hints": [r"top", r"榜", r"盘点", r"list"],
    },
    {
        "id": "encyclopedia",
        "name": "百科科普文",
        "desc": "概念定义、原理科普",
        "write_tip": "开篇「是指/定义为」，再边界条件与常见误解，便于做知识卡片引用。",
        "patterns": [
            r"(是指|定义为|所谓|指的是|原理|概念|什么是)",
            r"(百科|科普|入门)",
        ],
        "url_hints": [r"百科", r"wiki", r"科普", r"是什么"],
    },
]

# 被引用段落本身的「引用友好格式」
CITATION_FORMATS: list[dict[str, Any]] = [
    {
        "id": "claim_first",
        "name": "结论先行句",
        "desc": "段首即结论，后面才展开",
        "write_tip": "每段第一句写成可独立成立的结论，AI 最常摘这一句。",
        "patterns": [
            r"^(总之|综上|结论是|一句话|总的来说|因此|所以|建议|更适合)",
            r"^(.*?是|.*?为|.*?达).{0,20}(。|$)",
        ],
    },
    {
        "id": "fact_number",
        "name": "可核验事实句",
        "desc": "含明确数字、价格、日期、参数",
        "write_tip": "把关键数字写进完整句子，避免只放在表格无人读的单元格里。",
        "patterns": [
            r"\d+(\.\d+)?\s*%",
            r"\d+(\.\d+)?\s*(万|亿|元|公里|小时|天|款|项|km|kWh|GB|英寸)",
            r"(20\d{2}\s*年|\d+\s*月\s*\d+\s*日)",
            r"约\s*\d+",
        ],
    },
    {
        "id": "qa_pair",
        "name": "问答对格式",
        "desc": "问句 + 直接答句",
        "write_tip": "小标题用原问题，紧跟一句短答，再展开；短答最容易被引用。",
        "patterns": [
            r"[？?].{0,8}(答[:：]|是|可以|不能|建议)",
            r"(问[:：]|Q[:：]).{2,40}(答[:：]|A[:：])",
            r"(吗|呢|如何|怎么|多少).{0,6}[？?]",
        ],
    },
    {
        "id": "numbered_block",
        "name": "编号要点块",
        "desc": "1.2.3. 或条目化要点",
        "write_tip": "每条只写一个观点，条目标题可被单独摘取。",
        "patterns": [
            r"(^|\n)\s*([（(]?\d+[）).、]|[-•·*]|\d+、)",
            r"第[一二三四五六七八九十\d]+[点条项]",
        ],
    },
    {
        "id": "quote_style",
        "name": "引述摘录式",
        "desc": "带引号/「」的可摘原文",
        "write_tip": "关键表述用引号标出完整短句，方便模型整段引用。",
        "patterns": [
            r"[「『“\"].{6,80}[」』”\"]",
            r"(据|官方|其表示|指出)[:：].{6,60}",
        ],
    },
    {
        "id": "compare_clause",
        "name": "对比句式",
        "desc": "A 比 B / 更… / 不如…",
        "write_tip": "对比结论写成单句：对象 + 维度 + 胜负 + 适用场景。",
        "patterns": [
            r"(相比|比起|对比|优于|不如|更适合|胜在|弱在)",
            r".{1,20}(比|VS|vs).{1,20}(更|较)",
        ],
    },
    {
        "id": "definition_clause",
        "name": "定义句式",
        "desc": "是指 / 定义为 / 所谓",
        "write_tip": "知识类问题优先用定义句开场，后面再举例。",
        "patterns": [
            r"(是指|定义为|所谓|指的是|可以理解为|本质上是)",
        ],
    },
    {
        "id": "short_standalone",
        "name": "短独立段落",
        "desc": "短、完整、不依赖上下文",
        "write_tip": "控制在 40–90 字的完整句段，避免代词悬空（「这个/上述」）。",
        "patterns": [],  # 用长度启发式
    },
]


def _blob(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


def detect_article_types(
    *,
    title: str = "",
    segment: str = "",
    url: str = "",
    question: str = "",
    site_name: str = "",
) -> list[str]:
    text = _blob(title, segment, question, site_name)
    path = ""
    try:
        path = (urlparse(url).path or "") + " " + (urlparse(url).query or "")
    except Exception:
        path = url or ""
    hits: list[tuple[str, float]] = []
    for t in ARTICLE_TYPES:
        score = 0.0
        for pat in t["patterns"]:
            if re.search(pat, text, re.I):
                score += 1.0
        for pat in t.get("url_hints") or []:
            if re.search(pat, path, re.I) or re.search(pat, url, re.I):
                score += 0.8
        if score > 0:
            hits.append((t["name"], score))
    hits.sort(key=lambda x: -x[1])
    names = [n for n, _ in hits]
    return names or ["综合资讯文"]


def detect_citation_formats(segment: str, title: str = "") -> list[str]:
    """识别被引段落本身更像哪种「易引用格式」。"""
    text = (segment or title or "").strip()
    if not text:
        return []
    hits: list[str] = []
    for fmt in CITATION_FORMATS:
        if fmt["id"] == "short_standalone":
            # 短且无明显悬空指代
            if 18 <= len(text) <= 100 and not re.search(r"^(这|那|其|上述|该|此)\S{0,6}", text):
                hits.append(fmt["name"])
            continue
        for pat in fmt["patterns"]:
            if re.search(pat, text, re.I | re.M):
                hits.append(fmt["name"])
                break
    return hits or ["普通陈述句"]


def _meta_by_name(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for x in items:
        if x["name"] == name:
            return x
    return {}


def aggregate_named_rank(
    rows: list[dict[str, Any]],
    *,
    key: str,
    catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """对带 weight 与名称列表字段的样本做加权排行。"""
    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        names = r.get(key) or []
        if isinstance(names, str):
            names = [names]
        w = float(r.get("weight") or 0)
        if w <= 0 or not names:
            continue
        # 主类型全权重，其余次类型打折，避免一条样本刷多类
        for i, name in enumerate(names):
            factor = 1.0 if i == 0 else 0.45
            totals[name] += w * factor
            counts[name] += 1
    total_w = sum(totals.values()) or 1.0
    out = []
    for name, score in sorted(totals.items(), key=lambda x: -x[1]):
        meta = _meta_by_name(catalog, name)
        out.append(
            {
                "name": name,
                "score": round(score, 3),
                "share": round(score / total_w, 3),
                "count": counts[name],
                "tip": meta.get("write_tip") or "写成可独立摘取的完整事实句。",
                "desc": meta.get("desc") or "",
            }
        )
    return out


def recommend_article_plan(
    article_type_rank: list[dict[str, Any]],
    citation_format_rank: list[dict[str, Any]],
    site_query: str,
) -> list[str]:
    tips: list[str] = []
    if not article_type_rank and not citation_format_rank:
        tips.append(f'「{site_query}」暂无足够样本判断适合写什么类型文章。')
        return tips

    if article_type_rank:
        top = article_type_rank[:3]
        tips.append(
            "该站更容易被引用的文章类型（时间加权）："
            + "、".join(f"{x['name']}({int(x['share']*100)}%)" for x in top)
            + "。"
        )
        tips.append(f"优先可写：【{top[0]['name']}】——{top[0]['tip']}")
        if len(top) > 1:
            tips.append(f"次选可写：【{top[1]['name']}】——{top[1]['tip']}")

    if citation_format_rank:
        topf = citation_format_rank[:3]
        tips.append(
            "被引段落更常见的引用友好格式："
            + "、".join(f"{x['name']}({int(x['share']*100)}%)" for x in topf)
            + "。"
        )
        for x in topf[:2]:
            tips.append(f"· 格式「{x['name']}」：{x['tip']}")

    # 组合成稿配方
    at = article_type_rank[0]["name"] if article_type_rank else "综合资讯文"
    fm = [x["name"] for x in citation_format_rank[:2]] or ["可核验事实句"]
    tips.append(
        f"成稿配方建议：以「{at}」为主干，段内多用「{' + '.join(fm)}」，"
        "让每段都有一句可被 AI 直接摘走的话。"
    )
    return tips
