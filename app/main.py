from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.agent_train import list_packs, load_pack, run_monthly_training
from app.analyze import analyze_site_citations, normalize_site_query
from app.article_gen import generate_article_from_analysis
from app.auth import (
    clear_session_cookie,
    get_role,
    is_public_path,
    path_allowed_for_role,
    read_session,
    set_request_user,
    set_session_cookie,
    verify_user,
)
from app.config import ROOT, settings
from app.detector import is_running, start_detection
from app.doubao import probe_collect_agent
from app.export_doc import EXPORT_DIR, list_recent_exports
from app.hermes import hermes_configured
from app.knowledge import KnowledgeError, kb_configured, upload_file_bytes, upload_local_path
from app.runtime_settings import (
    get_collect_agent,
    get_token_rates,
    get_train_settings,
    save_overrides,
)
from app.run_score import analyze_and_save_run


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_db()
    yield


app = FastAPI(title="GEO 文档检测工具", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
# Python 3.14 + Jinja2 缓存键会触发 TypeError，关闭缓存
templates.env.cache = None


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if is_public_path(path):
        return await call_next(request)
    sess = read_session(request)
    user = sess.get("user")
    role = sess.get("role") or (get_role(str(user)) if user else "")
    if user:
        request.state.user = user
        request.state.role = role
        set_request_user(str(user), str(role))
        if not path_allowed_for_role(path, str(role)):
            # 无权限：回首页
            if path.startswith("/api/"):
                return JSONResponse({"detail": "无权限", "ok": False}, status_code=403)
            return RedirectResponse("/", status_code=303)
        return await call_next(request)
    set_request_user("")
    if path.startswith("/api/"):
        return JSONResponse({"detail": "未登录", "ok": False}, status_code=401)
    nxt = path
    if request.url.query:
        nxt = f"{path}?{request.url.query}"
    return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)


def _split_lines(text: str) -> list[str]:
    """长串粘贴：每个换行一条问题；忽略空行；去掉行首序号如 1. / 1、 / （1）。"""
    lines: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^\s*[(（]?\s*\d+\s*[)）\.．、:：]\s*", "", line).strip()
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return lines


def _split_csv(text: str) -> list[str]:
    parts: list[str] = []
    for chunk in (text or "").replace("，", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


async def _page_context(request: Request | None = None, **extra: Any) -> dict[str, Any]:
    sess = read_session(request) if request is not None else {}
    user = str(sess.get("user") or "")
    if not user and request is not None:
        user = str(getattr(request.state, "user", "") or "")
    role = str(sess.get("role") or (get_role(user) if user else ""))
    owner = user if role == "user" else None

    brands = await db.list_brands(owner_username=owner) if role == "user" else []
    brand_ids = {int(b["id"]) for b in brands}
    questions = await db.list_questions()
    if role == "user":
        questions = [q for q in questions if int(q.get("brand_id") or 0) in brand_ids]
    citations = await db.list_citations(limit=100)
    if role == "user":
        citations = [c for c in citations if int(c.get("brand_id") or 0) in brand_ids]
    sites = await db.list_distinct_sites()  # 投放分析用全站数据
    training = await db.get_latest_agent_training()
    pack = load_pack()
    last_error = await db.get_latest_ask_error()
    detection_runs = await db.list_detection_runs(
        limit=40,
        owner_username=owner if role == "user" else None,
    )
    # 顶部状态：用户只看自己的最近任务
    run = detection_runs[0] if (role == "user" and detection_runs) else (
        await db.get_latest_run() if role != "admin" else None
    )
    orphan_citations = await db.list_orphan_citations(limit=80) if role == "user" else []
    ctx = {
        "nav": "home",
        "brands": brands,
        "questions": questions,
        "question_counts": await db.count_questions_by_brand(),
        "brand_questions": [],
        "question": None,
        "citations": citations,
        "detection_runs": detection_runs,
        "orphan_citations": orphan_citations,
        "ask_logs": [],
        "run_citations": [],
        "question_blocks": [],
        "run_orphan_citations": [],
        "sites": sites,
        "run": run,
        "running": is_running(),
        "analysis": None,
        "analyze_site": "",
        "training": training,
        "pack_manifest": (pack or {}).get("manifest"),
        "packs": list_packs()[:6],
        "article_result": None,
        "article_site": "",
        "article_text": "",
        "train_message": None,
        "questions_message": None,
        "last_ask_error": (last_error or {}).get("error_message"),
        "ark_model": get_collect_agent()["ark_model"],
        "collect_agent": get_collect_agent(),
        "train_settings": get_train_settings(),
        "settings_message": None,
        "settings_ok": False,
        "probe_result": None,
        "hermes_ready": hermes_configured(),
        "hermes_model": settings.hermes_model,
        "generated": None,
        "gen_topic": "",
        "gen_brand": "",
        "gen_notes": "",
        "gen_word_count": 1200,
        "recent_exports": list_recent_exports(),
        "kb_ready": kb_configured(),
        "kb_base_url": settings.kb_base_url,
        "kb_purpose": settings.kb_purpose,
        "kb_message": None,
        "kb_result": None,
        "brand": None,
        "brand_message": None,
        "brand_form": None,
        "current_user": user or None,
        "current_role": role or None,
        "token_rates": get_token_rates(),
        "usage_months": [],
        "usage_by_month": {},
        "usage_entries": {},
        "usage_total_own": 0,
        "usage_users": [],
        "usage_user_months": {},
    }
    ctx.update(extra)
    return ctx


async def _render(request: Request, template: str = "home.html", **extra: Any):
    """兼容新版 Starlette：TemplateResponse(request, name, context)。"""
    ctx = await _page_context(request, **extra)
    sess = read_session(request)
    ctx["current_user"] = sess.get("user") or getattr(request.state, "user", None)
    ctx["current_role"] = sess.get("role") or getattr(request.state, "role", None) or (
        get_role(str(ctx["current_user"])) if ctx.get("current_user") else None
    )
    return templates.TemplateResponse(request, template, ctx)


async def _owned_brand(brand_id: int, request: Request) -> dict[str, Any] | None:
    brand = await db.get_brand(brand_id)
    if not brand:
        return None
    user = str(read_session(request).get("user") or "")
    role = str(read_session(request).get("role") or get_role(user))
    if role == "admin":
        return None  # 管理员不做采集侧品牌操作
    if brand.get("owner_username") != user:
        return None
    return brand


async def _owned_run(run_id: int, request: Request) -> dict[str, Any] | None:
    run = await db.get_run(run_id)
    if not run:
        return None
    user = str(read_session(request).get("user") or "")
    role = str(read_session(request).get("role") or get_role(user))
    if role != "user":
        return None
    bid = run.get("brand_id")
    if bid:
        return run if await _owned_brand(int(bid), request) else None
    owned = await db.list_detection_runs(limit=200, owner_username=user)
    if any(int(r["id"]) == int(run_id) for r in owned):
        return run
    return None


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = Query("/")):
    if read_session(request).get("user"):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"login_error": None, "next_url": next or "/", "username": ""},
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    next_url = (next or "/").strip() or "/"
    if not next_url.startswith("/"):
        next_url = "/"
    if verify_user(username, password):
        uname = username.strip()
        role = get_role(uname)
        resp = RedirectResponse(next_url, status_code=303)
        set_session_cookie(resp, {"user": uname, "role": role})
        return resp
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "login_error": "账号或密码错误。",
            "next_url": next_url,
            "username": username.strip(),
        },
        status_code=401,
    )


@app.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    clear_session_cookie(resp)
    return resp


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return await _render(request, "home.html", nav="home")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    role = str(read_session(request).get("role") or get_role(str(read_session(request).get("user") or "")))
    tpl = "settings_admin.html" if role == "admin" else "settings.html"
    return await _render(request, tpl, nav="settings")


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(
    request: Request,
    ark_api_key: str = Form(""),
    ark_model: str = Form(""),
    ark_base_url: str = Form(""),
    ask_interval_sec: float = Form(2.0),
    rate_collect_ask: float = Form(1.0),
    rate_collect_probe: float = Form(1.0),
    rate_collect_search: float = Form(0.35),
    rate_article_gen: float = Form(0.8),
    train_lookback_days: int = Form(365),
    train_note_default: str = Form(""),
):
    role = str(read_session(request).get("role") or get_role(str(read_session(request).get("user") or "")))
    try:
        if role == "admin":
            save_overrides(
                {
                    "train_lookback_days": train_lookback_days,
                    "train_note_default": train_note_default,
                }
            )
            msg = "月度训练 Agent 设置已保存。"
            tpl = "settings_admin.html"
        else:
            save_overrides(
                {
                    "ark_api_key": ark_api_key,
                    "ark_model": ark_model or get_collect_agent()["ark_model"],
                    "ark_base_url": ark_base_url or get_collect_agent()["ark_base_url"],
                    "ask_interval_sec": ask_interval_sec,
                    "token_rates": {
                        "collect_ask": rate_collect_ask,
                        "collect_probe": rate_collect_probe,
                        "collect_search": rate_collect_search,
                        "article_gen": rate_article_gen,
                    },
                }
            )
            msg = "采集检测 Agent 设置已保存。"
            tpl = "settings.html"
        ok = True
    except Exception as e:
        msg = f"保存失败：{e}"
        ok = False
        tpl = "settings_admin.html" if role == "admin" else "settings.html"
    return await _render(
        request,
        tpl,
        nav="settings",
        settings_message=msg,
        settings_ok=ok,
        collect_agent=get_collect_agent(),
        train_settings=get_train_settings(),
        token_rates=get_token_rates(),
        ark_model=get_collect_agent()["ark_model"],
    )


@app.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request):
    sess = read_session(request)
    user = str(sess.get("user") or "")
    role = str(sess.get("role") or get_role(user))
    if role == "admin":
        users = await db.list_token_usage_users()
        usage_user_months: dict[str, list] = {}
        usage_by_month: dict[str, list] = {}
        usage_entries: dict[str, list] = {}
        total_own = 0
        for u in users:
            uname = u["username"]
            total_own += int(u.get("own_tokens") or 0)
            months = await db.list_token_usage_months_for_user(
                "" if uname == "(未知)" else uname, limit=24
            )
            # 未知账号对应空 username
            key_user = "" if uname == "(未知)" else uname
            usage_user_months[uname] = months
            for m in months:
                mid = m["month_id"]
                mk = f"{uname}:{mid}"
                acts = await db.list_token_usage_by_activity(mid, username=key_user)
                usage_by_month[mk] = acts
                for a in acts:
                    ek = f"{uname}:{mid}:{a['activity']}"
                    usage_entries[ek] = await db.list_token_usage_entries(
                        mid, activity=a["activity"], username=key_user, limit=200
                    )
        return await _render(
            request,
            "usage_admin.html",
            nav="usage",
            usage_users=users,
            usage_user_months=usage_user_months,
            usage_by_month=usage_by_month,
            usage_entries=usage_entries,
            usage_total_own=total_own,
            token_rates=get_token_rates(),
        )

    months = await db.list_token_usage_months(limit=24, username=user)
    usage_by_month: dict[str, list] = {}
    usage_entries: dict[str, list] = {}
    total_own = 0
    for m in months:
        mid = m["month_id"]
        total_own += int(m.get("own_tokens") or 0)
        acts = await db.list_token_usage_by_activity(mid, username=user)
        usage_by_month[mid] = acts
        for a in acts:
            key = f"{mid}:{a['activity']}"
            usage_entries[key] = await db.list_token_usage_entries(
                mid, activity=a["activity"], username=user, limit=300
            )
    return await _render(
        request,
        "usage.html",
        nav="usage",
        usage_months=months,
        usage_by_month=usage_by_month,
        usage_entries=usage_entries,
        usage_total_own=total_own,
        token_rates=get_token_rates(),
    )


@app.post("/settings/test", response_class=HTMLResponse)
async def settings_test(
    request: Request,
    ark_api_key: str = Form(""),
    ark_model: str = Form(""),
    ark_base_url: str = Form(""),
    ask_interval_sec: float = Form(2.0),
):
    """用表单当前值探测连通性（API Key 留空则用已保存配置）。不自动保存。"""
    role = str(read_session(request).get("role") or get_role(str(read_session(request).get("user") or "")))
    if role == "admin":
        return RedirectResponse("/settings", status_code=303)
    _ = ask_interval_sec
    result = await probe_collect_agent(
        api_key=ark_api_key,
        model=ark_model,
        base_url=ark_base_url,
    )
    agent = dict(get_collect_agent())
    if ark_model.strip():
        agent["ark_model"] = ark_model.strip()
    if ark_base_url.strip():
        agent["ark_base_url"] = ark_base_url.strip().rstrip("/")
    return await _render(
        request,
        "settings.html",
        nav="settings",
        settings_message=None,
        settings_ok=False,
        probe_result=result,
        collect_agent=agent,
        ark_model=agent["ark_model"],
    )


@app.get("/collect", response_class=HTMLResponse)
async def collect_page(request: Request):
    return await _render(request, "collect.html", nav="collect")


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int):
    run = await _owned_run(run_id, request)
    if not run:
        return RedirectResponse("/collect", status_code=303)
    analysis = run.get("analysis")
    if not analysis and run.get("status") == "done":
        try:
            analysis = await analyze_and_save_run(run_id)
            run = await db.get_run(run_id) or run
        except Exception:
            analysis = None
    ask_logs = await db.list_ask_logs_for_run(run_id)
    run_citations = await db.list_citations(run_id=run_id, limit=5000)
    cites_by_qid: dict[Any, list[dict[str, Any]]] = {}
    cites_by_qtext: dict[str, list[dict[str, Any]]] = {}
    for c in run_citations:
        qid = c.get("question_id")
        if qid is not None:
            cites_by_qid.setdefault(qid, []).append(c)
        qt = (c.get("question_text") or "").strip()
        if qt:
            cites_by_qtext.setdefault(qt, []).append(c)

    question_blocks: list[dict[str, Any]] = []
    used_cite_ids: set[int] = set()
    for a in ask_logs:
        qid = a.get("question_id")
        qt = (a.get("question_text") or "").strip()
        cites = list(cites_by_qid.get(qid) or [])
        if not cites and qt:
            cites = list(cites_by_qtext.get(qt) or [])
        for c in cites:
            if c.get("id") is not None:
                used_cite_ids.add(int(c["id"]))
        question_blocks.append({"ask": a, "citations": cites})

    orphan_citations = [c for c in run_citations if c.get("id") not in used_cite_ids]
    return await _render(
        request,
        "run_detail.html",
        nav="collect",
        run=run,
        analysis=analysis or run.get("analysis"),
        ask_logs=ask_logs,
        run_citations=run_citations,
        question_blocks=question_blocks,
        run_orphan_citations=orphan_citations,
    )


@app.post("/runs/{run_id}/analyze", response_class=HTMLResponse)
async def run_analyze(request: Request, run_id: int):
    run = await _owned_run(run_id, request)
    if not run:
        return RedirectResponse("/collect", status_code=303)
    try:
        await analyze_and_save_run(run_id, force=True)
    except Exception:
        pass
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/analyze")
async def analyze_post(site: str = Form(...)):
    site_raw = site.strip()
    if not site_raw:
        return RedirectResponse("/analyze", status_code=303)
    return RedirectResponse(
        url=f"/analyze?site={quote(site_raw)}#analyze-result",
        status_code=303,
    )


@app.get("/analyze", response_class=HTMLResponse)
async def analyze_get(request: Request, site: str = Query("")):
    site_raw = site.strip()
    if not site_raw:
        return await _render(request, "analyze.html", nav="analyze")

    site_q = normalize_site_query(site_raw) or site_raw
    matched = await db.search_citations_by_site(site_q)
    if not matched and site_q != site_raw:
        matched = await db.search_citations_by_site(site_raw)
    analysis = analyze_site_citations(site_raw, matched)
    return await _render(
        request,
        "analyze.html",
        nav="analyze",
        analysis=analysis,
        analyze_site=site_raw,
    )


@app.post("/generate/article", response_class=HTMLResponse)
async def generate_article(
    request: Request,
    site: str = Form(...),
    topic: str = Form(...),
    brand: str = Form(""),
    notes: str = Form(""),
    word_count: int = Form(1200),
    export_docx: str = Form(""),
    export_pdf: str = Form(""),
):
    site_raw = site.strip()
    site_q = normalize_site_query(site_raw) or site_raw
    matched = await db.search_citations_by_site(site_q)
    if not matched and site_q != site_raw:
        matched = await db.search_citations_by_site(site_raw)
    analysis = analyze_site_citations(site_raw, matched)
    formats: list[str] = []
    if export_docx:
        formats.append("docx")
    if export_pdf:
        formats.append("pdf")
    if not formats:
        formats = ["docx", "pdf"]
    generated = await generate_article_from_analysis(
        analysis,
        site=site_raw,
        topic=topic,
        brand=brand,
        notes=notes,
        word_count=word_count,
        export_formats=formats,
    )
    return await _render(
        request,
        "analyze.html",
        nav="analyze",
        analysis=analysis,
        analyze_site=site_raw,
        generated=generated,
        gen_topic=topic.strip(),
        gen_brand=brand.strip(),
        gen_notes=notes.strip(),
        gen_word_count=word_count,
    )


@app.get("/exports/{filename}")
async def download_export(filename: str):
    """下载 data/exports 下的成稿。"""
    safe = Path(filename).name
    path = ROOT / "data" / "exports" / safe
    if not path.is_file() or not path.resolve().is_relative_to((ROOT / "data" / "exports").resolve()):
        return HTMLResponse("文件不存在", status_code=404)
    media = "application/octet-stream"
    if safe.endswith(".docx"):
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif safe.endswith(".pdf"):
        media = "application/pdf"
    return FileResponse(path, media_type=media, filename=safe)


@app.get("/agent", response_class=HTMLResponse)
async def agent_page(request: Request):
    return await _render(request, "agent.html", nav="agent")


@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request):
    return await _render(request, "knowledge.html", nav="knowledge")


@app.post("/knowledge/upload", response_class=HTMLResponse)
async def knowledge_upload(
    request: Request,
    file: UploadFile = File(...),
    purpose: str = Form(""),
):
    purpose_v = (purpose or "").strip() or settings.kb_purpose
    raw = await file.read()
    name = Path(file.filename or "upload.bin").name
    try:
        result = await upload_file_bytes(
            filename=name,
            content=raw,
            purpose=purpose_v,
            content_type=file.content_type,
        )
        msg = result.get("message")
        ok = True
    except KnowledgeError as e:
        result = {"ok": False, "message": str(e)}
        msg = str(e)
        ok = False
    return await _render(
        request,
        "knowledge.html",
        nav="knowledge",
        kb_message=msg,
        kb_result={**result, "ok": ok},
    )


@app.post("/knowledge/upload-export", response_class=HTMLResponse)
async def knowledge_upload_export(
    request: Request,
    filename: str = Form(...),
    purpose: str = Form(""),
    site: str = Form(""),
):
    purpose_v = (purpose or "").strip() or settings.kb_purpose
    safe = Path(filename).name
    path = EXPORT_DIR / safe
    try:
        if not path.is_file() or not path.resolve().is_relative_to(EXPORT_DIR.resolve()):
            raise KnowledgeError("导出文件不存在或路径非法。")
        result = await upload_local_path(path, purpose=purpose_v)
        msg = result.get("message")
        ok = True
    except KnowledgeError as e:
        result = {"ok": False, "message": str(e)}
        msg = str(e)
        ok = False

    site_raw = (site or "").strip()
    if site_raw:
        site_q = normalize_site_query(site_raw) or site_raw
        matched = await db.search_citations_by_site(site_q)
        if not matched and site_q != site_raw:
            matched = await db.search_citations_by_site(site_raw)
        return await _render(
            request,
            "analyze.html",
            nav="analyze",
            analysis=analyze_site_citations(site_raw, matched),
            analyze_site=site_raw,
            kb_message=msg,
            kb_result={**result, "ok": ok},
        )
    return await _render(
        request,
        "knowledge.html",
        nav="knowledge",
        kb_message=msg,
        kb_result={**result, "ok": ok},
    )


@app.post("/agent/train", response_class=HTMLResponse)
async def agent_train(request: Request, note: str = Form("")):
    from datetime import datetime, timedelta, timezone

    lookback = int(get_train_settings().get("train_lookback_days") or 365)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback)).strftime("%Y-%m-%d")
    citations = await db.list_all_citations_for_training()
    citations = [
        c
        for c in citations
        if str(c.get("created_at") or "")[:10] >= cutoff or not c.get("created_at")
    ]
    result = await run_monthly_training(citations, note=note.strip())
    if result.get("ok"):
        man = result.get("manifest") or {}
        await db.save_agent_training(
            month_id=result["month_id"],
            status="ok",
            example_count=int(man.get("example_count") or 0),
            site_count=int(man.get("site_count") or 0),
            total_weight=float(man.get("total_weight") or 0),
            pack_dir=result.get("pack_dir"),
            note=note.strip(),
            message=result.get("message") or "",
        )
    else:
        await db.save_agent_training(
            month_id=result.get("month_id") or "",
            status="error",
            message=result.get("message") or "训练失败",
            note=note.strip(),
        )
    return await _render(request, "agent.html", nav="agent", train_message=result.get("message"))


@app.get("/brands/new", response_class=HTMLResponse)
async def brand_new_page(request: Request):
    return await _render(request, "brand_new.html", nav="collect")


@app.post("/brands")
async def add_brand(
    request: Request,
    name: str = Form(...),
    aliases: str = Form(""),
    official_domains: str = Form(""),
):
    user = str(read_session(request).get("user") or "")
    try:
        await db.create_brand(
            name,
            _split_csv(aliases),
            _split_csv(official_domains),
            owner_username=user,
        )
    except Exception as e:
        return await _render(
            request,
            "brand_new.html",
            nav="collect",
            brand_message=f"添加失败：{e}",
            brand_form={
                "name": name,
                "aliases": aliases,
                "official_domains": official_domains,
            },
        )
    return RedirectResponse("/collect", status_code=303)


@app.get("/brands/{brand_id}/edit", response_class=HTMLResponse)
async def brand_edit_page(request: Request, brand_id: int):
    brand = await _owned_brand(brand_id, request)
    return await _render(
        request,
        "brand_edit.html",
        nav="collect",
        brand=brand,
    )


@app.post("/brands/{brand_id}/edit", response_class=HTMLResponse)
async def brand_edit_save(
    request: Request,
    brand_id: int,
    name: str = Form(...),
    aliases: str = Form(""),
    official_domains: str = Form(""),
):
    existing = await _owned_brand(brand_id, request)
    if not existing:
        return await _render(
            request,
            "brand_edit.html",
            nav="collect",
            brand=None,
            brand_message="品牌不存在或无权编辑。",
        )
    await db.update_brand(
        brand_id,
        name,
        _split_csv(aliases),
        _split_csv(official_domains),
    )
    brand = await db.get_brand(brand_id)
    return await _render(
        request,
        "brand_edit.html",
        nav="collect",
        brand=brand,
        brand_message="已保存。",
    )


@app.post("/brands/{brand_id}/delete")
async def remove_brand(request: Request, brand_id: int):
    brand = await _owned_brand(brand_id, request)
    if brand:
        await db.delete_brand(brand_id)
    return RedirectResponse("/collect", status_code=303)


@app.get("/brands/{brand_id}/questions", response_class=HTMLResponse)
async def brand_questions_page(request: Request, brand_id: int):
    brand = await _owned_brand(brand_id, request)
    brand_questions = await db.list_questions(brand_id) if brand else []
    return await _render(
        request,
        "brand_questions.html",
        nav="collect",
        brand=brand,
        brand_questions=brand_questions,
    )


@app.post("/brands/{brand_id}/questions", response_class=HTMLResponse)
async def brand_questions_add(
    request: Request,
    brand_id: int,
    questions: str = Form(...),
):
    brand = await _owned_brand(brand_id, request)
    if not brand:
        return await _render(
            request,
            "brand_questions.html",
            nav="collect",
            brand=None,
            brand_questions=[],
            questions_message="品牌不存在或无权操作。",
        )
    lines = _split_lines(questions)
    count = await db.create_questions_bulk(brand_id, lines)
    brand_questions = await db.list_questions(brand_id)
    return await _render(
        request,
        "brand_questions.html",
        nav="collect",
        brand=brand,
        brand_questions=brand_questions,
        questions_message=(
            f"已添加 {count} 条问题。"
            if count
            else "没有有效问题，请确认每行一条。"
        ),
    )


@app.post("/questions")
async def add_questions(
    request: Request,
    brand_id: int = Form(...),
    questions: str = Form(...),
):
    brand = await _owned_brand(brand_id, request)
    if not brand:
        return RedirectResponse("/collect", status_code=303)
    lines = _split_lines(questions)
    await db.create_questions_bulk(brand_id, lines)
    return RedirectResponse(f"/brands/{brand_id}/questions", status_code=303)


@app.get("/questions/{question_id}/edit", response_class=HTMLResponse)
async def question_edit_page(request: Request, question_id: int):
    question = await db.get_question(question_id)
    if question and not await _owned_brand(int(question["brand_id"]), request):
        question = None
    return await _render(
        request,
        "question_edit.html",
        nav="collect",
        question=question,
    )


@app.post("/questions/{question_id}/edit", response_class=HTMLResponse)
async def question_edit_save(
    request: Request,
    question_id: int,
    text: str = Form(...),
):
    existing = await db.get_question(question_id)
    if not existing or not await _owned_brand(int(existing["brand_id"]), request):
        return await _render(
            request,
            "question_edit.html",
            nav="collect",
            question=None,
            questions_message="问题不存在或无权编辑。",
        )
    await db.update_question_text(question_id, text)
    question = await db.get_question(question_id)
    return await _render(
        request,
        "question_edit.html",
        nav="collect",
        question=question,
        questions_message="已保存。",
    )


@app.post("/questions/{question_id}/delete")
async def remove_question(request: Request, question_id: int, next: str = Form("")):
    q = await db.get_question(question_id)
    if q and await _owned_brand(int(q["brand_id"]), request):
        await db.delete_question(question_id)
    if next.startswith("/brands/") and next.endswith("/questions"):
        return RedirectResponse(next, status_code=303)
    if q:
        return RedirectResponse(f"/brands/{q['brand_id']}/questions", status_code=303)
    return RedirectResponse("/collect", status_code=303)


@app.post("/detect")
async def detect(request: Request, brand_id: str = Form(...), force_all: str = Form("")):
    if not brand_id.strip():
        return RedirectResponse("/collect", status_code=303)
    bid = int(brand_id)
    brand = await _owned_brand(bid, request)
    if not brand:
        return RedirectResponse("/collect", status_code=303)
    if force_all.strip() in ("1", "on", "true", "yes"):
        await db.reset_questions_for_redetect(bid)
    result = await start_detection(brand_id=bid)
    if not result.get("ok"):
        return await _render(
            request,
            "brand_questions.html",
            nav="collect",
            brand=brand,
            brand_questions=await db.list_questions(bid),
            questions_message=result.get("message") or "无法开始检测",
        )
    return RedirectResponse(f"/brands/{bid}/questions", status_code=303)


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    run = await db.get_latest_run()
    return {"running": is_running(), "run": run}


@app.get("/api/citations")
async def api_citations(brand_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    return await db.list_citations(brand_id=brand_id, limit=limit)


@app.get("/api/questions")
async def api_questions(brand_id: int | None = None) -> list[dict[str, Any]]:
    return await db.list_questions(brand_id)


@app.get("/api/analyze")
async def api_analyze(site: str) -> dict[str, Any]:
    site_raw = site.strip()
    site_q = normalize_site_query(site_raw) or site_raw
    matched = await db.search_citations_by_site(site_q)
    if not matched and site_q != site_raw:
        matched = await db.search_citations_by_site(site_raw)
    return analyze_site_citations(site_raw, matched)


@app.post("/api/agent/train")
async def api_agent_train(note: str = "") -> dict[str, Any]:
    citations = await db.list_all_citations_for_training()
    result = await run_monthly_training(citations, note=note)
    if result.get("ok"):
        man = result.get("manifest") or {}
        await db.save_agent_training(
            month_id=result["month_id"],
            status="ok",
            example_count=int(man.get("example_count") or 0),
            site_count=int(man.get("site_count") or 0),
            total_weight=float(man.get("total_weight") or 0),
            pack_dir=result.get("pack_dir"),
            note=note,
            message=result.get("message") or "",
        )
    return result


@app.post("/api/citations/{citation_id}/level")
async def api_set_citation_level(citation_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    level = str(payload.get("content_level") or payload.get("level") or "").strip()
    if not level:
        return {"ok": False, "message": "缺少 content_level"}
    await db.update_citation_level(citation_id, level)
    return {"ok": True, "id": citation_id, "content_level": level}
