"""知识库文件上传：POST /v1/files（multipart）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from app.config import settings


class KnowledgeError(Exception):
    pass


def kb_configured() -> bool:
    return bool((settings.kb_base_url or "").strip())


def _files_url() -> str:
    base = (settings.kb_base_url or "").rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/files"
    return f"{base}/v1/files"


async def upload_file_bytes(
    *,
    filename: str,
    content: bytes,
    purpose: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    if not kb_configured():
        raise KnowledgeError("未配置知识库地址：请在 .env 设置 KB_BASE_URL=http://127.0.0.1:8686")
    if not content:
        raise KnowledgeError("文件内容为空。")

    purpose = (purpose or settings.kb_purpose or "brand-extras").strip() or "brand-extras"
    headers: dict[str, str] = {}
    if (settings.kb_api_key or "").strip():
        headers["Authorization"] = f"Bearer {settings.kb_api_key.strip()}"

    mime = content_type or _guess_mime(filename)
    files = {"file": (filename, content, mime)}
    data = {"purpose": purpose}

    try:
        async with httpx.AsyncClient(timeout=float(settings.kb_timeout_sec)) as client:
            resp = await client.post(_files_url(), files=files, data=data, headers=headers)
    except httpx.ConnectError as e:
        raise KnowledgeError(
            f"无法连接知识库（{settings.kb_base_url}）。请确认服务已在 8686 端口运行。详情：{e}"
        ) from e
    except httpx.TimeoutException as e:
        raise KnowledgeError("上传知识库超时，可增大 KB_TIMEOUT_SEC。") from e
    except httpx.HTTPError as e:
        raise KnowledgeError(f"上传失败：{e}") from e

    if resp.status_code >= 400:
        detail = (resp.text or "")[:400]
        raise KnowledgeError(f"知识库返回 HTTP {resp.status_code}：{detail}")

    try:
        payload = resp.json()
        response_text = json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        payload = {"raw": (resp.text or "")[:500]}
        response_text = str(payload.get("raw") or "")

    return {
        "ok": True,
        "filename": filename,
        "purpose": purpose,
        "response": payload,
        "response_text": response_text,
        "message": f"已上传「{filename}」到知识库（purpose={purpose}）。",
    }


async def upload_local_path(path: Path, *, purpose: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise KnowledgeError(f"文件不存在：{path.name}")
    return await upload_file_bytes(
        filename=path.name,
        content=path.read_bytes(),
        purpose=purpose,
    )


def _guess_mime(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if name.endswith(".xls"):
        return "application/vnd.ms-excel"
    if name.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if name.endswith(".pdf"):
        return "application/pdf"
    if name.endswith(".txt"):
        return "text/plain"
    if name.endswith(".md"):
        return "text/markdown"
    if name.endswith(".csv"):
        return "text/csv"
    return "application/octet-stream"
