"""采集检测模型客户端：支持火山方舟 Responses，以及 Agicto 等 OpenAI 兼容 Chat。"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.runtime_settings import get_collect_agent


class DoubaoError(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def detect_protocol(base_url: str) -> str:
    """ark_responses = 方舟 /responses；openai_chat = /chat/completions。"""
    b = (base_url or "").lower()
    if "volces.com" in b or "ark.cn-beijing" in b or b.rstrip("/").endswith("/api/v3"):
        return "ark_responses"
    return "openai_chat"


def _looks_like_url(value: str) -> bool:
    v = (value or "").strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def _json_error_message(payload: Any) -> str | None:
    """Agicto 等网关常返回 HTTP 200 + {\"error\": {...}}。"""
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if err is None:
        return None
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or ""
        msg = err.get("message") or err.get("msg") or str(err)
        return f"{code}: {msg}".strip(": ")
    return str(err)


def _site_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host or ""
    except Exception:
        return ""


def _extract_text_and_citations(payload: dict[str, Any]) -> dict[str, Any]:
    texts: list[str] = []
    cites: list[dict[str, Any]] = []
    queries: list[str] = []
    seen_urls: set[str] = set()

    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue

        if item.get("type") == "web_search_call":
            action = item.get("action") or {}
            q = action.get("query")
            if q:
                queries.append(str(q))

        if item.get("type") != "message":
            continue

        for ann in item.get("annotations") or []:
            _collect_citation(ann, cites, seen_urls)

        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text") or content.get("output_text") or ""
            if text:
                texts.append(str(text))
            for ann in content.get("annotations") or []:
                _collect_citation(ann, cites, seen_urls)

    answer = "\n".join(texts).strip()
    for c in cites:
        start = c.get("start_index")
        end = c.get("end_index")
        if answer and isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(answer):
            c["segment"] = answer[start:end]
        else:
            c["segment"] = c.get("title") or ""

    return {
        "answer_text": answer,
        "citations": cites,
        "search_queries": queries,
        "searched": bool(queries)
        or any(
            (i.get("type") == "web_search_call") for i in (payload.get("output") or []) if isinstance(i, dict)
        ),
    }


def _extract_openai_chat(payload: dict[str, Any]) -> dict[str, Any]:
    texts: list[str] = []
    cites: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content.strip())
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    t = part.get("text") or part.get("content") or ""
                    if t:
                        texts.append(str(t))
                    for ann in part.get("annotations") or []:
                        _collect_citation(ann, cites, seen_urls)
                elif isinstance(part, str) and part.strip():
                    texts.append(part.strip())
        # 部分思考模型正文为空，答案在 reasoning_content
        if not texts:
            for key in ("reasoning_content", "reasoning", "refusal"):
                alt = msg.get(key)
                if isinstance(alt, str) and alt.strip():
                    texts.append(alt.strip())
                    break
        for ann in msg.get("annotations") or []:
            _collect_citation(ann, cites, seen_urls)

    answer = "\n".join(texts).strip()
    for c in cites:
        start = c.get("start_index")
        end = c.get("end_index")
        if answer and isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(answer):
            c["segment"] = answer[start:end]
        else:
            c["segment"] = c.get("snippet") or c.get("title") or ""

    return {
        "answer_text": answer,
        "citations": cites,
        "search_queries": [],
        "searched": bool(cites),
    }


def _collect_citation(ann: dict[str, Any], cites: list[dict[str, Any]], seen_urls: set[str]) -> None:
    if not isinstance(ann, dict):
        return
    if ann.get("type") not in ("url_citation", "citation"):
        if "url_citation" not in ann and not ann.get("url"):
            return

    uc = ann.get("url_citation") if isinstance(ann.get("url_citation"), dict) else {}
    url = ann.get("url") or uc.get("url") or ""
    url = str(url).strip()
    if url and url in seen_urls:
        return
    if url:
        seen_urls.add(url)

    cites.append(
        {
            "title": ann.get("title") or uc.get("title") or "",
            "url": url,
            "site_name": uc.get("site_name") or ann.get("site_name") or _site_from_url(url),
            "start_index": ann.get("start_index", uc.get("start_index")),
            "end_index": ann.get("end_index", uc.get("end_index")),
            "segment": "",
            "snippet": ann.get("snippet") or uc.get("snippet") or "",
        }
    )


def _normalize_search_hits(payload: Any) -> list[dict[str, Any]]:
    rows: list[Any] = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("data", "results", "items", "organic", "webPages"):
            v = payload.get(key)
            if isinstance(v, list):
                rows = v
                break
            if isinstance(v, dict) and isinstance(v.get("value"), list):
                rows = v["value"]
                break
        if not rows and isinstance(payload.get("result"), list):
            rows = payload["result"]

    cites: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or item.get("href") or "").strip()
        title = str(item.get("title") or item.get("name") or "").strip()
        snippet = str(
            item.get("snippet")
            or item.get("description")
            or item.get("content")
            or item.get("summary")
            or ""
        ).strip()
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        if not (url or title or snippet):
            continue
        cites.append(
            {
                "title": title,
                "url": url,
                "site_name": _site_from_url(url),
                "segment": snippet or title,
                "snippet": snippet,
            }
        )
    return cites


async def _agicto_tool_search(
    client: httpx.AsyncClient,
    *,
    base: str,
    headers: dict[str, str],
    query: str,
) -> list[dict[str, Any]]:
    url = base.rstrip("/") + "/tool/search"
    body = {
        "crawl_results": 0,
        "max_results": 8,
        "query": query,
        "search_service": "bing",
    }
    try:
        resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError:
        return []
    if resp.status_code >= 400:
        return []
    try:
        payload = resp.json()
    except Exception:
        return []
    if _json_error_message(payload):
        return []
    return _normalize_search_hits(payload)


def _validate_credentials(cfg: dict[str, Any]) -> None:
    key = (cfg.get("ark_api_key") or "").strip()
    if not key or key.startswith("your_"):
        raise DoubaoError(
            "未配置采集 Agent：请到「设置」填写 API Key（Agicto / 方舟密钥），不要填网址。",
            retryable=False,
        )
    if _looks_like_url(key):
        raise DoubaoError(
            "API Key 填成了网址。请把 Base URL 填 https://api.agicto.cn/v1，"
            "API Key 填 Agicto 控制台里的密钥（sk-…），两者不要填反。",
            retryable=False,
        )


async def ask_with_web_search(question: str) -> dict[str, Any]:
    """每个问题单独开一轮全新对话，不带历史。"""
    cfg = get_collect_agent()
    _validate_credentials(cfg)
    protocol = detect_protocol(cfg["ark_base_url"])
    if protocol == "openai_chat":
        return await _ask_openai_chat(question, cfg)
    return await _ask_ark_responses(question, cfg)


async def _ask_ark_responses(question: str, cfg: dict[str, Any]) -> dict[str, Any]:
    url = cfg["ark_base_url"].rstrip("/") + "/responses"
    headers = {
        "Authorization": f"Bearer {cfg['ark_api_key']}",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }
    body: dict[str, Any] = {
        "model": cfg["ark_model"],
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": question}],
            }
        ],
        "tools": [{"type": "web_search"}],
        "thinking": {"type": "disabled"},
        "stream": False,
        "store": False,
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=30.0)) as client:
            resp = await client.post(url, headers=headers, json=body)
            if resp.status_code == 400 and "store" in (resp.text or "").lower():
                body.pop("store", None)
                resp = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as e:
        raise DoubaoError(f"请求超时: {e}", retryable=True) from e
    except httpx.HTTPError as e:
        raise DoubaoError(f"网络错误: {e}", retryable=True) from e

    payload = _parse_response_or_raise(resp, cfg)
    result = _extract_text_and_citations(payload)
    try:
        from app.auth import get_request_user
        from app.token_usage import record_from_payload

        await record_from_payload(
            payload,
            activity="collect_ask",
            model=cfg.get("ark_model") or "",
            username=get_request_user(),
            detail=(question or "")[:200],
            fallback_text=(question or "") + "\n" + (result.get("answer_text") or ""),
        )
    except Exception:
        pass
    return result


async def _ask_openai_chat(question: str, cfg: dict[str, Any]) -> dict[str, Any]:
    base = cfg["ark_base_url"].rstrip("/")
    url = base + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['ark_api_key']}",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }
    body = {
        "model": cfg["ark_model"],
        "messages": [{"role": "user", "content": question}],
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=30.0)) as client:
            resp = await client.post(url, headers=headers, json=body)
            payload = _parse_response_or_raise(resp, cfg)
            result = _extract_openai_chat(payload)
            if not (result.get("answer_text") or "").strip():
                raise DoubaoError(
                    "模型返回成功但正文为空。请换一个 Agicto 可用模型，或到「设置」点测试连接确认预览有字。",
                    retryable=True,
                )
            try:
                from app.auth import get_request_user
                from app.token_usage import record_from_payload

                await record_from_payload(
                    payload,
                    activity="collect_ask",
                    model=cfg.get("ark_model") or "",
                    username=get_request_user(),
                    detail=(question or "")[:200],
                    fallback_text=(question or "") + "\n" + (result.get("answer_text") or ""),
                )
            except Exception:
                pass
            # Agicto：答案里若无引用，补拉联网搜索结果作为参考资料（失败不影响正文）
            if "agicto" in base.lower() and not result["citations"]:
                try:
                    hits = await asyncio.wait_for(
                        _agicto_tool_search(client, base=base, headers=headers, query=question),
                        timeout=25.0,
                    )
                except Exception:
                    hits = []
                if hits:
                    result["citations"] = hits
                    result["search_queries"] = [question]
                    result["searched"] = True
                    try:
                        from app.auth import get_request_user
                        from app.token_usage import estimate_search_tokens, record_usage

                        u = estimate_search_tokens(question)
                        await record_usage(
                            activity="collect_search",
                            model=cfg.get("ark_model") or "",
                            prompt_tokens=u["prompt_tokens"],
                            completion_tokens=0,
                            total_tokens=u["total_tokens"],
                            username=get_request_user(),
                            detail=f"搜索：{(question or '')[:160]}",
                        )
                    except Exception:
                        pass
            return result
    except DoubaoError:
        raise
    except httpx.TimeoutException as e:
        raise DoubaoError(f"请求超时: {e}", retryable=True) from e
    except httpx.HTTPError as e:
        raise DoubaoError(f"网络错误: {e}", retryable=True) from e


def _parse_response_or_raise(resp: httpx.Response, cfg: dict[str, Any]) -> dict[str, Any]:
    if resp.status_code in (401, 403):
        raise DoubaoError(f"鉴权失败 ({resp.status_code}): {resp.text[:300]}", retryable=False)
    if resp.status_code == 429:
        raise DoubaoError(f"触发限流: {resp.text[:300]}", retryable=True)
    if resp.status_code >= 500:
        raise DoubaoError(f"服务端错误 ({resp.status_code}): {resp.text[:300]}", retryable=True)
    if resp.status_code >= 400:
        raise DoubaoError(_friendly_api_error(resp.status_code, resp.text, cfg), retryable=False)

    try:
        payload = resp.json()
    except Exception as e:
        raise DoubaoError(f"响应不是合法 JSON: {e}", retryable=False) from e

    err = _json_error_message(payload)
    if err:
        lower = err.lower()
        retryable = "rate" in lower or "限流" in lower or "429" in lower
        if "invalid_api_key" in lower or "incorrect api key" in lower:
            raise DoubaoError(
                "API Key 无效：请到「设置」填写 Agicto / 方舟控制台的真实密钥，"
                f"不要把 Base URL 填进 API Key。详情：{err}",
                retryable=False,
            )
        raise DoubaoError(f"接口返回错误：{err}", retryable=retryable)

    return payload if isinstance(payload, dict) else {"raw": payload}


async def probe_collect_agent(
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """轻量连通性探测：鉴权 + 模型是否可用。"""
    cfg = get_collect_agent()
    key = (api_key or "").strip() or cfg["ark_api_key"]
    model_id = (model or "").strip() or cfg["ark_model"]
    base = ((base_url or "").strip() or cfg["ark_base_url"]).rstrip("/")
    protocol = detect_protocol(base)

    if not key or key.startswith("your_"):
        return {
            "ok": False,
            "message": "未配置 API Key，请先填写并保存。",
            "latency_ms": None,
            "model": model_id,
            "preview": "",
            "protocol": protocol,
        }
    if _looks_like_url(key):
        return {
            "ok": False,
            "message": (
                "API Key 看起来像网址。Base URL 应填 https://api.agicto.cn/v1，"
                "API Key 应填控制台密钥（通常以 sk- 开头），当前 Key 填反了所以没有回复。"
            ),
            "latency_ms": None,
            "model": model_id,
            "preview": "",
            "protocol": protocol,
        }
    if not model_id:
        return {
            "ok": False,
            "message": "未填写模型 / 接入点 ID。",
            "latency_ms": None,
            "model": model_id,
            "preview": "",
            "protocol": protocol,
        }
    if not base:
        return {
            "ok": False,
            "message": "未填写 API Base URL。",
            "latency_ms": None,
            "model": model_id,
            "preview": "",
            "protocol": protocol,
        }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }
    probe_cfg = {"ark_model": model_id, "ark_api_key": key, "ark_base_url": base}
    t0 = time.perf_counter()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0)) as client:
            if protocol == "openai_chat":
                resp = await client.post(
                    base + "/chat/completions",
                    headers=headers,
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": "请只回复两个字：连通"}],
                        "stream": False,
                    },
                )
            else:
                body: dict[str, Any] = {
                    "model": model_id,
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "请只回复两个字：连通"}],
                        }
                    ],
                    "thinking": {"type": "disabled"},
                    "stream": False,
                    "store": False,
                }
                resp = await client.post(base + "/responses", headers=headers, json=body)
                if resp.status_code == 400 and "store" in (resp.text or "").lower():
                    body.pop("store", None)
                    resp = await client.post(base + "/responses", headers=headers, json=body)
    except httpx.TimeoutException as e:
        return {
            "ok": False,
            "message": f"连接超时：{e}",
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "model": model_id,
            "preview": "",
            "protocol": protocol,
        }
    except httpx.HTTPError as e:
        return {
            "ok": False,
            "message": f"网络错误：{e}",
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "model": model_id,
            "preview": "",
            "protocol": protocol,
        }

    latency_ms = int((time.perf_counter() - t0) * 1000)
    try:
        payload = _parse_response_or_raise(resp, probe_cfg)
    except DoubaoError as e:
        return {
            "ok": False,
            "message": str(e),
            "latency_ms": latency_ms,
            "model": model_id,
            "preview": "",
            "protocol": protocol,
        }

    if protocol == "openai_chat":
        parsed = _extract_openai_chat(payload)
    else:
        parsed = _extract_text_and_citations(payload)
    preview = (parsed.get("answer_text") or "").strip()[:120]
    if not preview:
        return {
            "ok": False,
            "message": f"接口有响应但没有模型正文（{latency_ms} ms）。请检查模型 ID 是否可用。",
            "latency_ms": latency_ms,
            "model": model_id,
            "preview": "",
            "protocol": protocol,
        }

    proto_label = "OpenAI 兼容 Chat" if protocol == "openai_chat" else "方舟 Responses"
    try:
        from app.auth import get_request_user
        from app.token_usage import record_from_payload

        await record_from_payload(
            payload,
            activity="collect_probe",
            model=model_id,
            username=get_request_user(),
            detail="测试连接",
            fallback_text="连通\n" + preview,
        )
    except Exception:
        pass
    return {
        "ok": True,
        "message": f"已连通（{proto_label}）：模型 {model_id} 响应正常（{latency_ms} ms）。",
        "latency_ms": latency_ms,
        "model": model_id,
        "preview": preview,
        "protocol": protocol,
    }


def _friendly_api_error(status: int, text: str, cfg: dict[str, Any] | None = None) -> str:
    raw = (text or "")[:800]
    lower = raw.lower()
    model = (cfg or get_collect_agent()).get("ark_model") or settings.ark_model
    if "invalid_api_key" in lower:
        return (
            f"API Key 无效 ({status})：请填写控制台密钥，不要把 Base URL 填进 Key。"
            f"原始信息：{raw[:300]}"
        )
    if "modelnotopen" in lower or "has not activated" in lower:
        if "web search" in lower or "web_search" in lower or "content_plugin" in lower or "toolnotopen" in lower:
            return (
                f"联网搜索未开通 ({status})：请到控制台开通联网内容插件 "
                f"https://console.volcengine.com/common-buy/CC_content_plugin 。原始信息：{raw[:300]}"
            )
        return (
            f"模型未开通 ({status})：请到火山方舟控制台开通当前模型，"
            f"或在「设置」把模型改成已开通的 ep-xxxx。原始信息：{raw[:300]}"
        )
    if "toolnotopen" in lower:
        return (
            f"工具未开通 ({status})：若提示 web search，请开通联网内容插件 "
            f"https://console.volcengine.com/common-buy/CC_content_plugin 。原始信息：{raw[:300]}"
        )
    if "invalidendpointormodel" in lower or "does not exist" in lower:
        return (
            f"模型或接入点不存在 ({status})：当前模型={model} 无效或无权限。"
            f"请在控制台开通后于「设置」填写正确模型名或 ep-xxxx。原始信息：{raw[:300]}"
        )
    return f"请求失败 ({status}): {raw[:500]}"
