"""Locate the sibling knowledge-base folder on Windows and macOS."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KB_REL = Path("数据库测试") / "数据库测试"


def find_kb_root() -> Path | None:
    """Return the knowledge-base directory if it exists on this machine."""
    candidates: list[Path] = []
    env = os.environ.get("GEO_KB_ROOT", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    home = Path.home()
    candidates.extend(
        [
            ROOT.parent / KB_REL,
            home / "OneDrive" / "Desktop" / KB_REL,
            home / "Desktop" / KB_REL,
        ]
    )
    cloud = home / "Library" / "CloudStorage"
    if cloud.is_dir():
        try:
            for one in sorted(cloud.glob("OneDrive*")):
                candidates.append(one / "Desktop" / KB_REL)
        except OSError:
            pass
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_dir():
            return candidate
    return None
