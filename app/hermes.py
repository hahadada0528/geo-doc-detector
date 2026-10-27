"""Hermes / wiki-assistant OpenAI-compatible API 客户端。"""
from __future__ import annotations

from typing import Any

import httpx

from app.config import settings


class HermesError(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def hermes_configured() -> bool:
    """只要配置了服务地址即可（你的 8080 接口可不带 API Key）。"""
    return bool((settings.hermes_base_url or "").strip())


def _base_v1() -> str:
    base = (settings.hermes_base_url or "").rstrip("/")
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


async def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.5,
    timeout: float | None = None,
) -> dict[str, Any]:
    """调用 POST /v1/chat/completions，返回 {content, raw, model}。"""
    if not hermes_configured():
        raise HermesError(
            "未配置写稿服务：请在 .env 设置 HERMES_BASE_URL（如 http://127.0.0.1:8080）。"
        )

    url = f"{_base_v1()}/chat/completions"
    payload = {
        "model": model or settings.hermes_model or "wiki-assistant",
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if (settings.hermes_api_key or "").strip():
        headers["Authorization"] = f"Bearer {settings.hermes_api_key.strip()}"

    wait = float(timeout if timeout is not None else settings.hermes_timeout_sec)

    try:
        async with httpx.AsyncClient(timeout=wait) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.ConnectError as e:
        raise HermesError(
            f"无法连接写稿服务（{settings.hermes_base_url}）。"
            f"请确认 FastAPI 已启动（如 uvicorn 监听 8080）。详情：{e}",
            retryable=True,
        ) from e
    except httpx.TimeoutException as e:
        raise HermesError(f"写稿服务响应超时（>{wait:.0f}s）。可增大 HERMES_TIMEOUT_SEC。", retryable=True) from e
    except httpx.HTTPError as e:
        raise HermesError(f"写稿请求失败：{e}", retryable=True) from e

    if resp.status_code == 401:
        detail = (resp.text or "")[:300]
        if "Nous Portal" in detail or "access token" in detail.lower():
            raise HermesError(
                "写稿上游 Hermes/Nous 未登录或凭证失效。"
                "请在本机终端执行：hermes portal login"
                "（登录后保持 Hermes Proxy 运行，再重新生成文章）。"
            )
        raise HermesError(
            "鉴权失败：若中继需要 Key，请在 .env 填写 HERMES_API_KEY；"
            "若走 Nous Portal，请执行 hermes portal login。"
        )
    if resp.status_code >= 400:
        detail = (resp.text or "")[:400]
        if "Nous Portal" in detail or "access token" in detail.lower():
            raise HermesError(
                "写稿上游 Hermes/Nous 凭证异常。"
                "请执行：hermes portal login ，然后重试生成。"
                f"详情：{detail}",
                retryable=True,
            )
        raise HermesError(f"写稿服务返回 HTTP {resp.status_code}：{detail}", retryable=resp.status_code >= 500)

    try:
        data = resp.json()
    except Exception as e:
        raise HermesError(f"写稿服务返回非 JSON：{(resp.text or '')[:200]}") from e

    content = _extract_content(data)
    if not content:
        raise HermesError("写稿服务未返回正文内容。")
    try:
        from app.auth import get_request_user
        from app.token_usage import record_from_payload

        brief = "\n".join(str(m.get("content") or "") for m in messages)
        await record_from_payload(
            data,
            activity="article_gen",
            model=str(payload["model"]),
            username=get_request_user(),
            detail="生成文章",
            fallback_text=brief + "\n" + content,
        )
    except Exception:
        pass
    return {"content": content, "raw": data, "model": payload["model"]}


def _extract_content(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message") or {}
        text = msg.get("content")
        if isinstance(text, str) and text.strip():
            return text.strip()
        if isinstance(text, list):
            parts = []
            for p in text:
                if isinstance(p, dict) and p.get("type") in ("text", "output_text"):
                    parts.append(str(p.get("text") or ""))
                elif isinstance(p, str):
                    parts.append(p)
            joined = "".join(parts).strip()
            if joined:
                return joined
    out = data.get("output_text")
    if isinstance(out, str) and out.strip():
        return out.strip()
    return ""
