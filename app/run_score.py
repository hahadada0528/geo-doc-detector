"""跑次分析编排：读库 → 打分 → 写回 detection_runs。"""
from __future__ import annotations

import logging
from typing import Any

from app import db
from app.run_analysis import analyze_run_payload

logger = logging.getLogger(__name__)


async def analyze_and_save_run(run_id: int, *, force: bool = False) -> dict[str, Any] | None:
    """对单次检测结果统一分析并入库。已有分析且非 force 则直接返回。"""
    run = await db.get_run(run_id)
    if not run:
        return None
    if run.get("analysis_json") and not force:
        return run.get("analysis")

    citations = await db.list_citations(run_id=run_id, limit=5000)
    ask_logs = await db.list_ask_logs_for_run(run_id)

    brand_name = (run.get("brand_name") or "").strip()
    aliases: list[str] = []
    brand_id = run.get("brand_id")
    if brand_id:
        brand = await db.get_brand(int(brand_id))
        if brand:
            brand_name = brand_name or brand.get("name") or ""
            aliases = brand.get("aliases") or []
    if not brand_name and citations:
        brand_name = (citations[0].get("brand_name") or "").strip()

    analysis = analyze_run_payload(
        brand_name=brand_name,
        aliases=aliases,
        ask_logs=ask_logs,
        citations=citations,
    )
    await db.save_run_analysis(run_id, analysis)
    logger.info(
        "run %s analyzed: coverage=%s priority=%s sentiment=%s",
        run_id,
        analysis.get("keyword_coverage"),
        analysis.get("answer_priority"),
        analysis.get("sentiment_score"),
    )
    return analysis
