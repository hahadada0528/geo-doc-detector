"""Token 用量统计：提取上游 usage，按活动倍率折算为「我方 Token」。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import db
from app.runtime_settings import get_token_rates

# 活动代码 → 中文名
ACTIVITY_LABELS: dict[str, str] = {
    "collect_ask": "采集检测 · 提问",
    "collect_probe": "设置 · 测试连接",
    "collect_search": "采集检测 · 联网搜索",
    "article_gen": "投放分析 · 生成文章",
}

DEFAULT_RATES: dict[str, float] = {
    "collect_ask": 1.0,
    "collect_probe": 1.0,
    "collect_search": 0.35,
    "article_gen": 0.8,
}


def month_id_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def extract_usage(payload: Any) -> dict[str, int]:
    """从 OpenAI / 方舟等响应中提取 token 用量。"""
    if not isinstance(payload, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    prompt = _as_int(
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or usage.get("prompt_token_count")
    )
    completion = _as_int(
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or usage.get("completion_token_count")
    )
    total = _as_int(usage.get("total_tokens") or usage.get("total_token_count"))
    if total <= 0:
        total = prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total if total > 0 else prompt + completion,
    }


def estimate_search_tokens(query: str) -> dict[str, int]:
    """联网搜索接口常无 usage：按查询长度估算。"""
    n = max(80, min(800, len(query or "") * 4 + 120))
    return {"prompt_tokens": n, "completion_tokens": 0, "total_tokens": n}


def estimate_text_tokens(text: str) -> int:
    """无 usage 时粗估：中文约 1.5 字/token，英文按 4 字符。"""
    t = text or ""
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    other = max(0, len(t) - cjk)
    return max(1, int(cjk / 1.5 + other / 4))


def to_own_tokens(raw_total: int, activity: str) -> int:
    rates = get_token_rates()
    rate = float(rates.get(activity, DEFAULT_RATES.get(activity, 1.0)))
    return max(0, int(round(max(0, raw_total) * rate)))


async def record_usage(
    *,
    activity: str,
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
    username: str = "",
    detail: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_total = int(total_tokens) if total_tokens is not None else int(prompt_tokens) + int(completion_tokens)
    if raw_total <= 0 and (prompt_tokens or completion_tokens):
        raw_total = int(prompt_tokens) + int(completion_tokens)
    own = to_own_tokens(raw_total, activity)
    row = {
        "month_id": month_id_now(),
        "activity": activity,
        "activity_label": ACTIVITY_LABELS.get(activity, activity),
        "model": model or "",
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": raw_total,
        "own_tokens": own,
        "username": username or "",
        "detail": (detail or "")[:500],
        "meta": meta or {},
    }
    await db.save_token_usage(row)
    return row


async def record_from_payload(
    payload: Any,
    *,
    activity: str,
    model: str = "",
    username: str = "",
    detail: str = "",
    meta: dict[str, Any] | None = None,
    fallback_text: str = "",
) -> dict[str, Any]:
    usage = extract_usage(payload)
    if usage["total_tokens"] <= 0 and fallback_text:
        est = estimate_text_tokens(fallback_text)
        usage = {"prompt_tokens": est, "completion_tokens": 0, "total_tokens": est}
    return await record_usage(
        activity=activity,
        model=model,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        username=username,
        detail=detail,
        meta=meta,
    )


def _as_int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0
