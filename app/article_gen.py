"""根据站点分析结果生成写作简报，并调用 Hermes 成稿。"""
from __future__ import annotations

import re
from typing import Any

from app.export_doc import export_article_files
from kb_paths import find_kb_root
from app.hermes import HermesError, chat_completion, hermes_configured
from app.writing_spec import (
    build_writing_spec,
    writing_spec_to_brief_json,
    writing_spec_to_markdown,
)


ANTI_AD_RULES = """
【硬性禁令 · 禁止输出广告文】
- 绝对禁止输出广告文、软文、种草安利文、品牌通稿、促销文。一旦像广告即判不合格，必须改写成资料文。
- 禁止出现或近义改写：立即购买、马上入手、限时优惠、下单、加购、点击购买、联系客服下单、代言口吻、号召行动。
- 禁止无依据最高级与安利腔：最好、第一、碾压、吊打、闭眼入、必入、强烈推荐（可改为中性「适合…场景」）。
- 允许：用可核验参数、场景结论、适用边界说明产品特点与优势；语气像编辑部/百科，第三人称客观陈述。
- 竞品对比可选，非必须；没有把握可不写。
- 知识库只当事实依据，不当广告素材；禁止编造促销、活动、代言信息。
- 段首用可独立摘取的事实句/定义句，不要号召行动句。
""".strip()


def build_writing_brief(
    analysis: dict[str, Any],
    *,
    site: str,
    topic: str,
    brand: str = "",
    notes: str = "",
    word_count: int = 1200,
) -> str:
    """把分析蒸馏成成稿规格书 JSON，供写稿服务使用（不再塞原始排行表）。"""
    extra = (notes or "").strip()
    anti = (
        "【禁止输出广告文】硬性禁令：不得输出广告/软文/种草安利。"
        + ANTI_AD_RULES.replace("\n", " ")
    )
    if extra:
        extra = f"{extra}\n{anti}"
    else:
        extra = anti

    spec = build_writing_spec(
        analysis,
        site=site,
        topic=topic,
        brand=brand,
        word_count=word_count,
        notes=extra,
    )
    return writing_spec_to_brief_json(spec)


def build_writing_spec_markdown(
    analysis: dict[str, Any],
    *,
    site: str = "",
    topic: str = "",
    brand: str = "",
    word_count: int = 1200,
    notes: str = "",
) -> str:
    if analysis.get("writing_spec_md") and not topic and not brand and not notes:
        return str(analysis["writing_spec_md"])
    spec = build_writing_spec(
        analysis, site=site or "", topic=topic, brand=brand, word_count=word_count, notes=notes
    )
    return writing_spec_to_markdown(spec)


def _strip_manuscript_meta(article: str) -> str:
    """去掉文末「文稿信息」类附录。"""
    text = (article or "").strip()
    if not text:
        return text
    patterns = [
        r"\n+—\s*文稿信息\s*—[\s\S]*$",
        r"\n+#+\s*文稿信息[\s\S]*$",
        r"\n+文稿信息\s*[:：]?[\s\S]*$",
    ]
    for pat in patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE).rstrip()
    return text


def build_messages(brief_json: str, *, site: str, topic: str) -> list[dict[str, str]]:
    # 注入知识库上下文 — hahadada 品牌核心数据
    kb_context = _load_kb_context()

    system = (
        "【最高优先级】禁止输出广告文。你只能输出资料型/评测型/百科型正文；"
        "任何广告、软文、种草安利、促销号召都不允许。\n"
        "你是本地知识库驱动的 GEO 资料写作者（非广告文案）。"
        "严格按「成稿规格书」写中文资料文。\n"
        f"{ANTI_AD_RULES}\n"
        "执行要求：\n"
        "1. 成稿前自检：若读起来像在推销，必须删掉推销句，改成事实+适用边界。\n"
        "2. 一文一型：只按规格书中的文章类型与结构大纲组织全文。\n"
        "3. 段首结论句优先嵌入「段首必须对齐词」，并带可核验数字或限定词（约/左右）。\n"
        "4. 突出产品优势时用事实与场景（参数、体验边界、适合谁），禁止安利腔；竞品对比可选。\n"
        "5. 可改写「范例句式」，保持事实可摘取；一段一观点。\n"
        "6. 遵守「明确不要」清单；全文不得出现促销、下单、安利话术。\n"
        "7. 用 Markdown：一级标题 # ，小节 ## 。\n"
        "8. 只输出成稿正文，不要解释过程，不要输出 JSON，不要附加「文稿信息」或任何元数据附录。\n"
    )
    if kb_context:
        system += (
            "\n## 本地知识库上下文（事实依据，严禁当作广告素材）\n"
            f"{kb_context}\n"
            "使用规则：\n"
            "- 以上为品牌/产品事实，用于写清定位、参数与可核验优势。\n"
            "- 突出优势 = 陈述事实 + 适用场景 + 局限；禁止写成推销/广告。\n"
            "- 若自然需要对比定位，可用中性维度一带而过；不要硬写竞品，更不要贬低他人。\n"
            "- 知识库未提供的促销、活动、代言信息一律不要写。\n"
            "- 以知识库事实为主，行业背景为辅；缺数不编造。\n"
        )
    user = (
        f"【禁止广告文】请为投放站点「{site}」撰写主题为「{topic}」的资料型文章。\n"
        f"硬性约束：禁止输出广告文/软文/种草安利；只用可核验事实说明产品特点与优势，并写清适用边界；竞品对比可选。\n"
        f"以下是成稿规格书（JSON），请严格按其中大纲与约束生成：\n\n{brief_json}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _load_kb_context() -> str:
    """从知识库加载 hahadada 品牌核心数据摘要，注入到 system prompt。"""
    try:
        kb_dir = find_kb_root()
        if kb_dir is None:
            return ""
        brand_page = (kb_dir / "brand" / "hahadada.md").read_text(encoding="utf-8")
        # 去掉 frontmatter，只保留正文摘要
        if brand_page.startswith("---"):
            parts = brand_page.split("---", 2)
            if len(parts) >= 3:
                brand_page = parts[2].strip()
        # 截取核心部分（概览 + 产品 + 渠道 + 供应链）
        lines = brand_page.split("\n")
        context_lines = []
        capture = False
        for line in lines:
            if line.startswith("## 概览"):
                capture = True
            if capture:
                context_lines.append(line)
            if line.startswith("## 联系方式"):
                break
        return "\n".join(context_lines[:80]) if context_lines else brand_page[:2000]
    except Exception:
        return ""


async def generate_article_from_analysis(
    analysis: dict[str, Any],
    *,
    site: str,
    topic: str,
    brand: str = "",
    notes: str = "",
    word_count: int = 1200,
    export_formats: list[str] | None = None,
) -> dict[str, Any]:
    topic = (topic or "").strip()
    site = (site or "").strip()
    if not site:
        return {"ok": False, "message": "缺少投放站点。"}
    if not topic:
        return {"ok": False, "message": "请填写文章主题。"}
    if not hermes_configured():
        return {
            "ok": False,
            "message": (
                "未配置写稿服务。请在 .env 设置 HERMES_BASE_URL=http://127.0.0.1:8080，"
                "HERMES_MODEL=wiki-assistant，并确保该 FastAPI 已启动。"
            ),
        }

    brief = build_writing_brief(
        analysis,
        site=site,
        topic=topic,
        brand=brand,
        notes=notes,
        word_count=word_count,
    )
    messages = build_messages(brief, site=site, topic=topic)
    try:
        result = await chat_completion(messages)
    except HermesError as e:
        return {"ok": False, "message": str(e), "brief": brief}

    article = result["content"]
    # 去掉模型偶发附带的文稿信息附录
    article = _strip_manuscript_meta(article)
    try:
        files = export_article_files(
            article,
            title=topic,
            formats=export_formats or ["docx", "pdf"],
        )
    except Exception as e:
        return {
            "ok": True,
            "article": article,
            "model": result.get("model"),
            "brief": brief,
            "files": {},
            "downloads": [],
            "message": f"文章已生成，但导出 Word/PDF 失败：{e}（请执行 python -m pip install python-docx reportlab 后重启）",
        }

    downloads = []
    for kind, rel in files.items():
        name = rel.replace("\\", "/").split("/")[-1]
        downloads.append(
            {
                "kind": "Word" if kind == "docx" else kind.upper(),
                "name": name,
                "href": f"/exports/{name}",
            }
        )

    return {
        "ok": True,
        "article": article,
        "model": result.get("model"),
        "brief": brief,
        "files": files,
        "downloads": downloads,
        "message": "已生成资料型文章。请点击下方按钮下载 Word / PDF。",
    }
