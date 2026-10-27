"""资料内容分级：用户反馈级、产品信息级等。"""
from __future__ import annotations

import re
from typing import Any

# 分级定义：越靠前越优先作为「主分级」候选（同分时）
CONTENT_LEVELS: list[dict[str, Any]] = [
    {
        "id": "user_feedback",
        "name": "用户反馈级",
        "desc": "真实用户评价、体验反馈、口碑吐槽，偏 UGC",
        "write_tip": "用第一人称或「用户反馈」口吻写具体场景体验，避免空泛好评；可引用痛点与对比感受。",
        "patterns": [
            r"(用户|买家|车主|粉丝|网友).{0,6}(反馈|评价|评论|口碑|吐槽|实测)",
            r"(我觉得|我感觉|用了|入手后|体验下来|真香|踩坑|后悔|推荐给)",
            r"(好评|差评|口碑|满意度|投诉|晒单|种草|拔草)",
            r"(实际使用|日常使用|开了.?天|用了.?个?月)",
        ],
    },
    {
        "id": "product_info",
        "name": "产品信息级",
        "desc": "参数、配置、规格、官方产品说明",
        "write_tip": "用可核验的参数表/规格句：型号、尺寸、续航、价格区间、核心配置，一句一事实。",
        "patterns": [
            r"(参数|配置|规格|型号|售价|定价|官方指导价)",
            r"(续航|电池|芯片|处理器|屏幕|分辨率|马力|扭矩|轴距)",
            r"(配备|搭载|采用|支持|标配|选装)",
            r"\d+(\.\d+)?\s*(kWh|km|mm|英寸|GB|TB|马力|万元)",
        ],
    },
    {
        "id": "review_compare",
        "name": "评测对比级",
        "desc": "专业评测、横评、优劣对比",
        "write_tip": "先给对比结论，再用 2–4 个维度（性能/价格/体验）展开，明确更适合谁。",
        "patterns": [
            r"(评测|横评|实测对比|深度体验|上手体验)",
            r"(对比|相比|比起|VS|vs|优缺点|哪个好|区别在于)",
            r"(胜在|弱在|更适合|不如|领先|落后)",
        ],
    },
    {
        "id": "howto_guide",
        "name": "教程攻略级",
        "desc": "操作步骤、使用方法、选购攻略",
        "write_tip": "按步骤编号写清「如何做/如何选」，每步可独立摘取，结尾给适用人群。",
        "patterns": [
            r"(教程|攻略|指南|步骤|方法|流程|怎么选|如何选|选购)",
            r"(首先|然后|接着|最后|第[一二三四五六七八九十\d]+[步点])",
            r"(操作|设置|开通|安装|注册)",
        ],
    },
    {
        "id": "news_info",
        "name": "新闻资讯级",
        "desc": "发布会、上市、动态消息",
        "write_tip": "写清时间点 + 事件（发布/上市/更新）+ 关键变化，避免评价性空话。",
        "patterns": [
            r"(发布|上市|亮相|官宣|即将|正式推出|新品)",
            r"(发布会|消息称|据悉|报道|资讯|动态)",
            r"(更新|升级|改款|迭代)",
        ],
    },
    {
        "id": "encyclopedia",
        "name": "百科知识级",
        "desc": "定义、概念解释、百科式说明",
        "write_tip": "开篇用「是指/定义为」给清晰定义，再补边界条件与常见误解。",
        "patterns": [
            r"(是指|定义为|所谓|指的是|可以理解为|本质上是)",
            r"(百科|概念|原理|含义|什么是)",
            r"(主要包括|通常包括|一般是指)",
        ],
    },
    {
        "id": "ranking_list",
        "name": "榜单推荐级",
        "desc": "排行榜、TOP 榜、盘点推荐",
        "write_tip": "用榜单结构：名次 + 一句话理由 + 适合谁；理由里放可核验卖点。",
        "patterns": [
            r"(TOP\s*\d+|排行|排名|推荐榜|盘点|榜单|精选清单)",
            r"(第一名|第二名|冠军|入选|热门推荐)",
        ],
    },
    {
        "id": "general",
        "name": "综合信息级",
        "desc": "未命中明确类型时的综合陈述",
        "write_tip": "改写成更明确的分级：产品参数、用户体验或评测对比，便于 AI 摘取。",
        "patterns": [],
    },
]

_LEVEL_BY_ID = {x["id"]: x for x in CONTENT_LEVELS}
_LEVEL_BY_NAME = {x["name"]: x for x in CONTENT_LEVELS}


def level_ids() -> list[str]:
    return [x["id"] for x in CONTENT_LEVELS]


def level_name(level_id: str) -> str:
    return _LEVEL_BY_ID.get(level_id, _LEVEL_BY_ID["general"])["name"]


def level_tip(level_id: str) -> str:
    return _LEVEL_BY_ID.get(level_id, _LEVEL_BY_ID["general"])["write_tip"]


def classify_content_level(
    text: str,
    *,
    url: str | None = None,
    site_name: str | None = None,
    manual_level: str | None = None,
) -> dict[str, Any]:
    """返回主分级 + 次分级得分。manual_level 可为 id 或中文名。"""
    if manual_level:
        mid = _normalize_manual(manual_level)
        meta = _LEVEL_BY_ID[mid]
        return {
            "level_id": mid,
            "level_name": meta["name"],
            "confidence": 1.0,
            "scores": {mid: 1.0},
            "source": "manual",
        }

    blob = " ".join(x for x in [text or "", url or "", site_name or ""] if x)
    scores: dict[str, float] = {}
    for lv in CONTENT_LEVELS:
        if lv["id"] == "general":
            continue
        score = 0.0
        for pat in lv["patterns"]:
            if re.search(pat, blob, re.I):
                score += 1.0
        if score > 0:
            scores[lv["id"]] = score

    # URL/站点启发式加权
    host = (url or "").lower()
    site = (site_name or "").lower()
    if any(k in host or k in site for k in ("zhihu", "知乎", "douban", "豆瓣", "xiaohongshu", "小红书", "weibo", "微博")):
        scores["user_feedback"] = scores.get("user_feedback", 0) + 0.6
    if any(k in host or k in site for k in ("官网", "official")) or site == "官网":
        scores["product_info"] = scores.get("product_info", 0) + 0.8
    if any(k in host for k in ("jd.com", "tmall", "taobao", "pinduoduo")):
        scores["product_info"] = scores.get("product_info", 0) + 0.4
        scores["user_feedback"] = scores.get("user_feedback", 0) + 0.3

    if not scores:
        meta = _LEVEL_BY_ID["general"]
        return {
            "level_id": "general",
            "level_name": meta["name"],
            "confidence": 0.2,
            "scores": {"general": 0.2},
            "source": "auto",
        }

    ranked = sorted(scores.items(), key=lambda x: (-x[1], level_ids().index(x[0])))
    top_id, top_score = ranked[0]
    total = sum(scores.values()) or 1.0
    meta = _LEVEL_BY_ID[top_id]
    return {
        "level_id": top_id,
        "level_name": meta["name"],
        "confidence": round(min(1.0, top_score / max(2.0, total) * 1.5), 3),
        "scores": {k: round(v, 3) for k, v in ranked},
        "source": "auto",
    }


def detect_content_levels(text: str, *, url: str | None = None, site_name: str | None = None) -> list[str]:
    """返回文中命中的全部分级名称（可多选），至少含主分级。"""
    result = classify_content_level(text, url=url, site_name=site_name)
    names = []
    for lid, score in (result.get("scores") or {}).items():
        if lid == "general" and len(result.get("scores") or {}) > 1:
            continue
        if score > 0:
            names.append(level_name(lid))
    if not names:
        names = [result["level_name"]]
    # 主分级置顶
    primary = result["level_name"]
    names = [primary] + [n for n in names if n != primary]
    return names


def _normalize_manual(value: str) -> str:
    v = (value or "").strip()
    if v in _LEVEL_BY_ID:
        return v
    if v in _LEVEL_BY_NAME:
        return _LEVEL_BY_NAME[v]["id"]
    # 容错：用户反馈 / 产品信息
    for lv in CONTENT_LEVELS:
        if v in lv["name"] or lv["name"].startswith(v):
            return lv["id"]
    return "general"


def aggregate_level_rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对带 weight + level_id 的样本做加权排行。"""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for r in rows:
        lid = r.get("level_id") or "general"
        w = float(r.get("weight") or 0)
        if w <= 0:
            continue
        totals[lid] = totals.get(lid, 0.0) + w
        counts[lid] = counts.get(lid, 0) + 1
    total_w = sum(totals.values()) or 1.0
    ranked = sorted(totals.items(), key=lambda x: -x[1])
    out = []
    for lid, score in ranked:
        meta = _LEVEL_BY_ID.get(lid, _LEVEL_BY_ID["general"])
        out.append(
            {
                "level_id": lid,
                "level_name": meta["name"],
                "score": round(score, 3),
                "share": round(score / total_w, 3),
                "count": counts.get(lid, 0),
                "tip": meta["write_tip"],
                "desc": meta["desc"],
            }
        )
    return out
