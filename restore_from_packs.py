"""从 agent_packs 的 weighted_examples.jsonl 恢复 brands / questions / citations。"""
from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path

from app import db
from app.config import ROOT

PACKS = [
    ROOT / "data" / "agent_packs" / "2026-08" / "weighted_examples.jsonl",
    ROOT / "data" / "agent_packs" / "2026-07" / "weighted_examples.jsonl",
]


def load_examples() -> list[dict]:
    seen: set[tuple] = set()
    rows: list[dict] = []
    for path in PACKS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            key = (
                (obj.get("brand_name") or "").strip().lower(),
                (obj.get("question_text") or "").strip(),
                (obj.get("url") or "").strip(),
                (obj.get("segment") or "").strip()[:120],
                (obj.get("site_name") or "").strip(),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(obj)
    return rows


async def restore() -> dict:
    await db.init_db()
    examples = load_examples()
    if not examples:
        return {"ok": False, "message": "训练包中没有可恢复样本。"}

    # backup empty/current db
    bak = ROOT / "data" / f"geo.db.bak_before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if db.DB_PATH.exists():
        shutil.copy2(db.DB_PATH, bak)

    # brand_name -> brand_id
    brand_ids: dict[str, int] = {}
    for ex in examples:
        name = (ex.get("brand_name") or "未知品牌").strip() or "未知品牌"
        key = name.lower()
        if key not in brand_ids:
            existing = [b for b in await db.list_brands() if b["name"].lower() == key]
            if existing:
                brand_ids[key] = int(existing[0]["id"])
            else:
                brand_ids[key] = await db.create_brand(name)

    # question_text+brand -> question_id
    q_ids: dict[tuple[str, str], int] = {}
    for ex in examples:
        brand = (ex.get("brand_name") or "未知品牌").strip() or "未知品牌"
        qtext = (ex.get("question_text") or "").strip()
        if not qtext:
            continue
        key = (brand.lower(), qtext)
        if key in q_ids:
            continue
        bid = brand_ids[brand.lower()]
        qid = await db.create_question(bid, qtext)
        await db.set_question_status(qid, "done")
        q_ids[key] = qid

    cite_rows: list[dict] = []
    for ex in examples:
        brand = (ex.get("brand_name") or "未知品牌").strip() or "未知品牌"
        qtext = (ex.get("question_text") or "").strip()
        if not qtext:
            continue
        qid = q_ids.get((brand.lower(), qtext))
        if not qid:
            continue
        site = (ex.get("site_name") or "").strip() or "未知来源"
        cite_rows.append(
            {
                "question_id": qid,
                "run_id": None,
                "question_text": qtext,
                "site_name": site,
                "is_official": site in ("官网", "官方网站") or "官网" in site,
                "url": ex.get("url") or "",
                "title": ex.get("title") or "",
                "segment": ex.get("segment") or "",
                "answer_excerpt": "",
                "content_level": ex.get("level_name") or ex.get("level_id") or "",
                "brand_name": brand,
                "added_at": ex.get("added_at"),
            }
        )

    await db.save_citations(cite_rows)

    brands_n = len(await db.list_brands())
    qs_n = len(await db.list_questions())
    cites_n = len(await db.list_citations(limit=20000))
    return {
        "ok": True,
        "backup": str(bak),
        "from_examples": len(examples),
        "brands": brands_n,
        "questions": qs_n,
        "citations": cites_n,
        "message": f"已从训练包恢复：品牌 {brands_n}、问题 {qs_n}、引用 {cites_n}。",
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(restore()), ensure_ascii=False, indent=2))
