"""本地启动：python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"""
from app.main import app

__all__ = ["app"]
