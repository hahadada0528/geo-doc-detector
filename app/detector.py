"""依次提问并落库引用。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app import db
from app.doubao import DoubaoError, ask_with_web_search
from app.levels import classify_content_level
from app.official import display_site_name
from app.runtime_settings import get_collect_agent
from app.run_score import analyze_and_save_run
from app.auth import get_request_user, set_request_user

logger = logging.getLogger(__name__)

_runner_lock = asyncio.Lock()
_current_task: asyncio.Task | None = None


def is_running() -> bool:
    return _current_task is not None and not _current_task.done()


async def start_detection(brand_id: int | None = None, question_ids: list[int] | None = None) -> dict[str, Any]:
    global _current_task
    async with _runner_lock:
        if is_running():
            return {"ok": False, "message": "已有检测任务在运行"}

        if question_ids:
            questions = []
            for qid in question_ids:
                q = await db.get_question(qid)
                if q:
                    questions.append(q)
        else:
            questions = await db.pending_questions(brand_id)

        if not questions:
            return {"ok": False, "message": "没有待检测的问题"}

        # 组名：第一次提问时间（开跑时刻）+ 品牌名
        primary_brand_id = questions[0].get("brand_id")
        brand_name = ""
        if primary_brand_id:
            brand = await db.get_brand(int(primary_brand_id))
            brand_name = (brand or {}).get("name") or ""
        if brand_id and not brand_name:
            brand = await db.get_brand(int(brand_id))
            brand_name = (brand or {}).get("name") or ""
            primary_brand_id = brand_id

        run_id = await db.create_run(
            len(questions),
            brand_id=int(primary_brand_id) if primary_brand_id else None,
            brand_name=brand_name,
        )
        username = get_request_user()
        _current_task = asyncio.create_task(_run(run_id, questions, username=username))
        return {"ok": True, "run_id": run_id, "total": len(questions)}


async def _run(run_id: int, questions: list[dict[str, Any]], *, username: str = "") -> None:
    """按队列逐题提问；每题独立新开对话，禁止共用短期记忆。"""
    if username:
        set_request_user(username)
    brand_cache: dict[int, dict[str, Any]] = {}
    done = 0
    try:
        for q in questions:
            qid = q["id"]
            brand_id = q["brand_id"]
            if brand_id not in brand_cache:
                brand = await db.get_brand(brand_id)
                if not brand:
                    await db.set_question_status(qid, "error")
                    await db.save_ask_log(qid, run_id, "error", error_message="品牌不存在")
                    done += 1
                    await db.bump_run(run_id, done)
                    continue
                brand_cache[brand_id] = brand
            brand = brand_cache[brand_id]

            await db.set_question_status(qid, "running")
            try:
                # 每题单独请求：无对话历史、无 previous_response_id
                logger.info("fresh conversation for question_id=%s", qid)
                result = await ask_with_web_search(q["text"])
                citations = result.get("citations") or []
                rows = []
                for c in citations:
                    site_name, is_official = display_site_name(
                        brand_name=brand["name"],
                        aliases=brand.get("aliases") or [],
                        official_domains=brand.get("official_domains") or [],
                        url=c.get("url"),
                        site_name=c.get("site_name"),
                        title=c.get("title"),
                    )
                    level = classify_content_level(
                        " ".join(
                            x
                            for x in [c.get("title") or "", c.get("segment") or "", q["text"]]
                            if x
                        ),
                        url=c.get("url"),
                        site_name=site_name,
                    )
                    rows.append(
                        {
                            "question_id": qid,
                            "run_id": run_id,
                            "question_text": q["text"],
                            "site_name": site_name,
                            "is_official": is_official,
                            "url": c.get("url"),
                            "title": c.get("title"),
                            "segment": c.get("segment") or "",
                            "answer_excerpt": (result.get("answer_text") or "")[:500],
                            "content_level": level["level_name"],
                            "brand_name": brand["name"],
                        }
                    )

                await db.save_citations(rows)
                await db.save_ask_log(
                    qid,
                    run_id,
                    "ok",
                    answer_text=result.get("answer_text") or "",
                    search_queries=result.get("search_queries") or [],
                    raw_citations=citations,
                )
                await db.set_question_status(qid, "done")
            except DoubaoError as e:
                logger.exception("ask failed qid=%s", qid)
                await db.save_ask_log(qid, run_id, "error", error_message=str(e))
                await db.set_question_status(qid, "error")
                if not e.retryable and ("ARK_API_KEY" in str(e) or "采集 Agent" in str(e)):
                    await db.finish_run(run_id, "error", str(e))
                    return
            except Exception as e:
                logger.exception("unexpected ask error qid=%s", qid)
                await db.save_ask_log(qid, run_id, "error", error_message=str(e))
                await db.set_question_status(qid, "error")

            done += 1
            await db.bump_run(run_id, done)
            await asyncio.sleep(get_collect_agent()["ask_interval_sec"])

        await db.finish_run(run_id, "done")
        # 提问结束后统一分析当次结果并入库
        try:
            await analyze_and_save_run(run_id, force=True)
        except Exception:
            logger.exception("post-run analysis failed run_id=%s", run_id)
    except Exception as e:
        logger.exception("detection run crashed")
        await db.finish_run(run_id, "error", str(e))
