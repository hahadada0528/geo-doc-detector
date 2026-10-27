"""Resolve and launch KB services for start_geo.bat (Unicode-safe).

Usage:
  python resolve_kb_dir.py relay   -> starts relay_server on port 8080
  python resolve_kb_dir.py wiki     -> starts api_server on port 8686
  python resolve_kb_dir.py check relay  -> prints "OK" if relay_server.py exists
  python resolve_kb_dir.py path relay   -> prints the KB dir path (for logging only)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from kb_paths import find_kb_root

HERMES_PY = Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent" / "venv" / "Scripts" / "python.exe"


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("")
        return 1

    cmd = args[0].strip().lower()
    kb_root = find_kb_root()

    if cmd == "check":
        which = args[1] if len(args) > 1 else "relay"
        if kb_root is None:
            print("MISSING")
        elif which == "relay":
            print("OK" if (kb_root / "relay_server.py").is_file() else "MISSING")
        elif which == "wiki":
            print("OK" if (kb_root / "api_server.py").is_file() else "MISSING")
        else:
            print("MISSING")
        return 0

    if cmd == "path":
        print("" if kb_root is None else str(kb_root))
        return 0 if kb_root is not None else 1

    if kb_root is None:
        print("MISSING", file=sys.stderr)
        return 1

    if cmd == "relay":
        relay_py = kb_root / "relay_server.py"
        if not relay_py.is_file():
            print("MISSING", file=sys.stderr)
            return 1
        py = str(HERMES_PY) if HERMES_PY.is_file() else sys.executable
        os.chdir(kb_root)
        os.execv(
            py,
            [py, "-m", "uvicorn", "relay_server:app",
             "--host", "127.0.0.1", "--port", "8080",
             "--app-dir", str(kb_root)],
        )

    if cmd == "wiki":
        api_py = kb_root / "api_server.py"
        if not api_py.is_file():
            print("MISSING", file=sys.stderr)
            return 1
        py = str(HERMES_PY) if HERMES_PY.is_file() else sys.executable
        os.chdir(kb_root)
        os.execv(
            py,
            [py, "-m", "uvicorn", "api_server:app",
             "--host", "127.0.0.1", "--port", "8686",
             "--app-dir", str(kb_root)],
        )

    print("")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
