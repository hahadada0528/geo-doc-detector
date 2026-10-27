"""本地 Agent 月度训练：从数据库自动导入引用，按时间权重生成训练包。"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.analyze import (
    analyze_site_citations,
    detect_forms,
    time_weight,
)
from app.config import ROOT
from app.levels import (
    CONTENT_LEVELS,
    aggregate_level_rank,
    classify_content_level,
)
from app.writing_spec import build_writing_spec, writing_spec_to_markdown

PACKS_DIR = ROOT / "data" / "agent_packs"
LATEST_POINTER = PACKS_DIR / "LATEST"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _month_id(now: datetime | None = None) -> str:
    now = now or _utc_now()
    return now.strftime("%Y-%m")


def _tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    words = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{2,}", text))
    return words


def normalize_site_for_training(citation: dict[str, Any]) -> str:
    """训练/分析用站点名：所有官网引用统一归入「官网」。"""
    raw = (citation.get("site_name") or "").strip()
    if citation.get("is_official") in (1, True, "1", "true", "True"):
        return "官网"
    low = raw.lower()
    if raw in ("官网", "官方网站", "官方站") or low in ("official", "official site"):
        return "官网"
    if "官网" in raw or "官方网站" in raw:
        return "官网"
    return raw or "未知来源"


def build_training_examples(citations: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """从引用库导入并附时间权重；每次训练按「当前时刻」重算权重。

    权重为 0 的超一年样本丢弃。官网（is_official / 站点名含官网）统一归入「官网」。
    """
    now = now or _utc_now()
    examples: list[dict[str, Any]] = []
    for c in citations:
        w = time_weight(c.get("added_at"), now)
        if w <= 0:
            continue
        segment = (c.get("segment") or "").strip()
        title = (c.get("title") or "").strip()
        text = segment or title
        if not text:
            continue
        site = normalize_site_for_training(c)
        forms = detect_forms(" ".join(x for x in [title, segment, c.get("answer_excerpt") or ""] if x))
        level = classify_content_level(
            " ".join(x for x in [title, segment, c.get("question_text") or ""] if x),
            url=c.get("url"),
            site_name=site,
            manual_level=c.get("content_level") or c.get("level_id"),
        )
        examples.append(
            {
                "id": c.get("id"),
                "site_name": site,
                "original_site_name": (c.get("site_name") or "").strip(),
                "is_official": site == "官网",
                "url": c.get("url") or "",
                "title": title,
                "segment": segment,
                "question_text": c.get("question_text") or "",
                "brand_name": c.get("brand_name") or "",
                "added_at": c.get("added_at"),
                "weight": w,
                "forms": forms or ["短句陈述"],
                "level_id": level["level_id"],
                "level_name": level["level_name"],
                "level_confidence": level["confidence"],
                "tokens": sorted(_tokenize(text)),
            }
        )
    examples.sort(key=lambda x: (-x["weight"], x.get("added_at") or ""))
    return examples


def build_site_profiles(examples: list[dict[str, Any]]) -> dict[str, Any]:
    by_site: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ex in examples:
        site = ex["site_name"] or "未知来源"
        by_site[site].append(ex)

    profiles: dict[str, Any] = {}
    for site, rows in by_site.items():
        # 复用 analyze 的形式排行逻辑（构造伪 citation）
        fake = [
            {
                "added_at": r["added_at"],
                "site_name": site,
                "title": r["title"],
                "segment": r["segment"],
                "question_text": r["question_text"],
                "answer_excerpt": "",
                "url": r["url"],
            }
            for r in rows
        ]
        analysis = analyze_site_citations(site, fake)
        level_rank = aggregate_level_rank(rows)
        # 每个分级下的高权重样本
        by_level: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_level[r.get("level_id") or "general"].append(r)
        level_examples = {}
        for lid, items in by_level.items():
            items_sorted = sorted(items, key=lambda x: -float(x["weight"]))[:5]
            level_examples[lid] = [
                {
                    "weight": x["weight"],
                    "segment": (x.get("segment") or "")[:200],
                    "title": (x.get("title") or "")[:100],
                    "forms": x.get("forms") or [],
                    "question_text": x.get("question_text"),
                }
                for x in items_sorted
            ]

        top_level = level_rank[0]["level_name"] if level_rank else "综合信息级"
        advice = list(analysis["advice"])
        if site == "官网":
            advice.insert(
                0,
                "「官网」为全品牌官方来源合并画像：写稿时优先可摘取的产品参数、FAQ 定义句与权威结论句。",
            )
        advice.insert(
            0 if site != "官网" else 1,
            f"该站近期被引资料主分级偏「{top_level}」（时间加权）；写稿时优先对齐该分级，再混入次级资料。",
        )
        if level_rank:
            mix = "、".join(f"{x['level_name']}({int(x['share']*100)}%)" for x in level_rank[:3])
            advice.insert(2 if site == "官网" else 1, f"建议资料配比（按加权占比）：{mix}。")

        profiles[site] = {
            "site": site,
            "is_official_bucket": site == "官网",
            "example_count": len(rows),
            "total_weight": analysis["total_weight"],
            "form_rank": analysis["form_rank"],
            "level_rank": level_rank,
            "level_examples": level_examples,
            "article_type_rank": analysis.get("article_type_rank") or [],
            "citation_format_rank": analysis.get("citation_format_rank") or [],
            "recommended_article_type": analysis.get("recommended_article_type"),
            "recommended_citation_formats": analysis.get("recommended_citation_formats") or [],
            "overlap_keywords": analysis.get("overlap_keywords") or [],
            "cited_keywords": analysis.get("cited_keywords") or [],
            "cited_phrases": analysis.get("cited_phrases") or [],
            "sentence_patterns": analysis.get("sentence_patterns") or [],
            "char_features": analysis.get("char_features") or [],
            "primary_level": top_level,
            "avg_segment_len": analysis["avg_segment_len"],
            "advice": advice,
            "top_examples": [
                {
                    "weight": r["weight"],
                    "segment": r["segment"][:240],
                    "title": r["title"][:120],
                    "question_text": r["question_text"],
                    "forms": r["forms"],
                    "level_id": r.get("level_id"),
                    "level_name": r.get("level_name"),
                    "original_site_name": r.get("original_site_name") or "",
                }
                for r in sorted(rows, key=lambda x: -x["weight"])[:12]
            ],
        }
        # 成稿规格：供写稿直接使用
        profile_for_spec = dict(profiles[site])
        profile_for_spec["samples"] = profile_for_spec.get("top_examples") or []
        wspec = build_writing_spec(profile_for_spec, site=site)
        profiles[site]["writing_spec"] = wspec
        profiles[site]["writing_spec_md"] = writing_spec_to_markdown(wspec)
    return profiles


def distill_agent_prompt(profiles: dict[str, Any], examples: list[dict[str, Any]], month_id: str) -> str:
    lines = [
        f"# GEO 本地写作 Agent（训练月：{month_id}）",
        "",
        "你是本地 GEO 写作助手。写稿时优先遵守各站「成稿规格书」（一文一型、结构大纲、对齐词、范例句），",
        "不要被零散统计表带偏。统计仅作背景。",
        "",
        "## 时间权重规则（训练用）",
        "- 1–30 天：1.0；31–60：0.8；61–90：0.65；91–180：0.45；181–365：0.2；更久不计",
        "",
        "## 资料分级体系",
        *[f"- {lv['name']}：{lv['desc']}" for lv in CONTENT_LEVELS],
        "",
        "## 成稿总原则",
        "- 资料型非广告；段首可摘取；一段一观点；给适用边界与局限",
        "- 官网画像为全品牌官方来源合并，优先参数/FAQ/权威结论句",
        "",
        "## 分站点成稿规格（按权重，官网优先）",
        "",
    ]
    ranked_sites = sorted(
        profiles.values(),
        key=lambda p: (
            0 if p.get("site") == "官网" else 1,
            -float(p.get("total_weight") or 0),
        ),
    )[:15]
    for p in ranked_sites:
        title = "官网（全品牌官方来源合并）" if p.get("site") == "官网" else p["site"]
        lines.append(f"### {title}")
        md = (p.get("writing_spec_md") or "").strip()
        if md:
            # 去掉重复的一级标题
            body = md
            if body.startswith("# 成稿规格书"):
                body = body.split("\n", 1)[-1].strip()
            lines.append(body)
        else:
            lines.append(f"- 样本 {p.get('example_count')}，主分级 {p.get('primary_level') or '—'}")
            lines.append(f"- 推荐类型 {p.get('recommended_article_type') or '—'}")
        lines.append("")

    lines.extend(
        [
            "## 成稿检查清单",
            "1. 是否按该站「一文一型」与结构大纲成文",
            "2. 段首是否复用对齐词并含数字/断言",
            "3. 是否出现范例句式的可摘取事实句",
            "4. 是否避开「明确不要」清单",
            "5. 结尾是否有适用场景或局限",
            "",
        ]
    )
    return "\n".join(lines)


def write_pack(
    *,
    month_id: str,
    examples: list[dict[str, Any]],
    profiles: dict[str, Any],
    note: str = "",
) -> Path:
    PACKS_DIR.mkdir(parents=True, exist_ok=True)
    pack_dir = PACKS_DIR / month_id
    pack_dir.mkdir(parents=True, exist_ok=True)

    # examples 里 tokens 是 list，jsonl 可写；体积大时保留
    examples_path = pack_dir / "weighted_examples.jsonl"
    with examples_path.open("w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    profiles_path = pack_dir / "site_profiles.json"
    profiles_path.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")

    prompt = distill_agent_prompt(profiles, examples, month_id)
    (pack_dir / "agent_prompt.md").write_text(prompt, encoding="utf-8")

    form_totals: dict[str, float] = defaultdict(float)
    for ex in examples:
        for form in ex["forms"]:
            form_totals[form] += float(ex["weight"])

    level_rank = aggregate_level_rank(examples)

    manifest = {
        "month_id": month_id,
        "trained_at": _utc_now().strftime("%Y-%m-%d %H:%M:%S"),
        "example_count": len(examples),
        "site_count": len(profiles),
        "total_weight": round(sum(float(e["weight"]) for e in examples), 3),
        "weight_rules": [
            {"range": "1-30", "weight": 1.0},
            {"range": "31-60", "weight": 0.8},
            {"range": "61-90", "weight": 0.65},
            {"range": "91-180", "weight": 0.45},
            {"range": "181-365", "weight": 0.2},
        ],
        "official_merged": True,
        "official_example_count": int((profiles.get("官网") or {}).get("example_count") or 0),
        "content_levels": [{"id": x["id"], "name": x["name"], "desc": x["desc"]} for x in CONTENT_LEVELS],
        "top_forms": [
            {"form": k, "score": round(v, 3)}
            for k, v in sorted(form_totals.items(), key=lambda x: -x[1])[:10]
        ],
        "top_levels": level_rank[:10],
        "note": note,
        "files": {
            "examples": "weighted_examples.jsonl",
            "profiles": "site_profiles.json",
            "prompt": "agent_prompt.md",
        },
    }
    (pack_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    LATEST_POINTER.write_text(month_id, encoding="utf-8")
    return pack_dir


def read_latest_month() -> str | None:
    if not LATEST_POINTER.exists():
        return None
    text = LATEST_POINTER.read_text(encoding="utf-8").strip()
    return text or None


def load_pack(month_id: str | None = None) -> dict[str, Any] | None:
    mid = month_id or read_latest_month()
    if not mid:
        return None
    pack_dir = PACKS_DIR / mid
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.exists():
        return None

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    profiles = {}
    profiles_path = pack_dir / "site_profiles.json"
    if profiles_path.exists():
        profiles = json.loads(profiles_path.read_text(encoding="utf-8"))

    examples: list[dict[str, Any]] = []
    examples_path = pack_dir / "weighted_examples.jsonl"
    if examples_path.exists():
        with examples_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    examples.append(json.loads(line))

    prompt = ""
    prompt_path = pack_dir / "agent_prompt.md"
    if prompt_path.exists():
        prompt = prompt_path.read_text(encoding="utf-8")

    return {
        "month_id": mid,
        "pack_dir": str(pack_dir),
        "manifest": manifest,
        "profiles": profiles,
        "examples": examples,
        "prompt": prompt,
    }


def list_packs() -> list[dict[str, Any]]:
    if not PACKS_DIR.exists():
        return []
    latest = read_latest_month()
    packs = []
    for p in sorted(PACKS_DIR.iterdir(), reverse=True):
        if not p.is_dir():
            continue
        man = p / "manifest.json"
        if not man.exists():
            continue
        data = json.loads(man.read_text(encoding="utf-8"))
        data["is_latest"] = data.get("month_id") == latest
        packs.append(data)
    return packs


async def run_monthly_training(
    citations: list[dict[str, Any]],
    *,
    note: str = "",
    month_id: str | None = None,
) -> dict[str, Any]:
    """每次点击均为全量重训：按当前时刻重算时间权重，覆盖写出本地 Agent 包。

    官网引用统一归入「官网」站点画像。
    """
    mid = month_id or _month_id()
    now = _utc_now()
    examples = build_training_examples(citations, now=now)
    if not examples:
        return {
            "ok": False,
            "message": "数据库中没有可训练的引用样本（时间权重为 0 或文本为空），请先跑检测采集。",
            "month_id": mid,
        }

    profiles = build_site_profiles(examples)
    pack_dir = write_pack(month_id=mid, examples=examples, profiles=profiles, note=note)
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    official_n = int((profiles.get("官网") or {}).get("example_count") or 0)
    return {
        "ok": True,
        "message": (
            f"已全量重训（权重按 {now.strftime('%Y-%m-%d %H:%M')} 重算）："
            f"{manifest['example_count']} 条样本，{manifest['site_count']} 个站点；"
            f"其中官网合并 {official_n} 条。"
        ),
        "month_id": mid,
        "pack_dir": str(pack_dir),
        "manifest": manifest,
    }
