"""运行时设置：覆盖 .env，供页面「设置」保存采集检测 Agent 等配置。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import ROOT, settings

SETTINGS_PATH = ROOT / "data" / "app_settings.json"

# 采集检测可写字段
COLLECT_KEYS = (
    "ark_api_key",
    "ark_model",
    "ark_base_url",
    "ask_interval_sec",
)

# Token 折算倍率（上游 raw token × 倍率 = 我方 Token）
TOKEN_RATE_KEYS = (
    "collect_ask",
    "collect_probe",
    "collect_search",
    "article_gen",
)
DEFAULT_TOKEN_RATES = {
    "collect_ask": 1.0,
    "collect_probe": 1.0,
    "collect_search": 0.35,
    "article_gen": 0.8,
}

TRAIN_KEYS = (
    "train_lookback_days",
    "train_note_default",
)
DEFAULT_TRAIN = {
    "train_lookback_days": 365,
    "train_note_default": "",
}


def _read_file() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_overrides() -> dict[str, Any]:
    return _read_file()


def save_overrides(updates: dict[str, Any]) -> dict[str, Any]:
    """合并写入；空字符串的 api_key 表示保持原值不改。"""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = _read_file()
    for k, v in updates.items():
        if k == "token_rates" and isinstance(v, dict):
            rates = dict(current.get("token_rates") or {})
            for rk, rv in v.items():
                if rk not in TOKEN_RATE_KEYS:
                    continue
                try:
                    rates[rk] = max(0.0, float(rv))
                except (TypeError, ValueError):
                    continue
            current["token_rates"] = rates
            continue
        if k in TRAIN_KEYS:
            if k == "train_lookback_days":
                try:
                    current[k] = max(30, min(3650, int(float(v))))
                except (TypeError, ValueError):
                    continue
            else:
                current[k] = str(v or "").strip()
            continue
        if k not in COLLECT_KEYS:
            continue
        if k == "ark_api_key" and (v is None or str(v).strip() == ""):
            continue
        if k == "ask_interval_sec":
            try:
                current[k] = float(v)
            except (TypeError, ValueError):
                continue
        else:
            current[k] = str(v).strip()
    SETTINGS_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def get_token_rates() -> dict[str, float]:
    ov = _read_file().get("token_rates") or {}
    out = dict(DEFAULT_TOKEN_RATES)
    if isinstance(ov, dict):
        for k in TOKEN_RATE_KEYS:
            if k in ov:
                try:
                    out[k] = max(0.0, float(ov[k]))
                except (TypeError, ValueError):
                    pass
    return out


def get_train_settings() -> dict[str, Any]:
    ov = _read_file()
    days = ov.get("train_lookback_days", DEFAULT_TRAIN["train_lookback_days"])
    try:
        days_i = max(30, min(3650, int(float(days))))
    except (TypeError, ValueError):
        days_i = int(DEFAULT_TRAIN["train_lookback_days"])
    return {
        "train_lookback_days": days_i,
        "train_note_default": str(ov.get("train_note_default") or ""),
        "source_file": _display_settings_path(),
    }


def get_collect_agent() -> dict[str, Any]:
    """采集检测有效配置：页面覆盖优先，否则 .env / 默认。"""
    ov = _read_file()
    key = str(ov.get("ark_api_key") or settings.ark_api_key or "").strip()
    model = str(ov.get("ark_model") or settings.ark_model or "").strip()
    base = str(ov.get("ark_base_url") or settings.ark_base_url or "").strip()
    interval = ov.get("ask_interval_sec", settings.ask_interval_sec)
    try:
        interval_f = float(interval)
    except (TypeError, ValueError):
        interval_f = float(settings.ask_interval_sec)
    return {
        "ark_api_key": key,
        "ark_model": model,
        "ark_base_url": base.rstrip("/"),
        "ask_interval_sec": max(0.0, interval_f),
        "ark_api_key_masked": _mask_key(key),
        "configured": bool(key)
        and not key.startswith("your_")
        and not key.lower().startswith("http://")
        and not key.lower().startswith("https://"),
        "key_looks_like_url": bool(key)
        and (key.lower().startswith("http://") or key.lower().startswith("https://")),
        "protocol": _detect_protocol(base),
        "source_file": _display_settings_path(),
    }


def _detect_protocol(base_url: str) -> str:
    b = (base_url or "").lower()
    if "volces.com" in b or "ark.cn-beijing" in b or b.rstrip("/").endswith("/api/v3"):
        return "ark_responses"
    return "openai_chat"


def _display_settings_path() -> str:
    try:
        return str(SETTINGS_PATH.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(SETTINGS_PATH)


def _mask_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return "（未配置）"
    if len(k) <= 8:
        return "*" * len(k)
    return f"{k[:4]}…{k[-4:]}"
