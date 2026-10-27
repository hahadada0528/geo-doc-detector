"""把成稿导出为 Word (.docx) / PDF。"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from app.config import ROOT
from kb_paths import find_kb_root

EXPORT_DIR = ROOT / "data" / "exports"


def list_recent_exports(limit: int = 12) -> list[dict[str, str]]:
    """最近导出的 Word/PDF，供页面直接下载。"""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    files = [
        p
        for p in EXPORT_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in {".docx", ".pdf", ".doc"}
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, str]] = []
    for p in files[:limit]:
        kind = "PDF" if p.suffix.lower() == ".pdf" else "Word"
        out.append(
            {
                "name": p.name,
                "kind": kind,
                "href": f"/exports/{p.name}",
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            }
        )
    return out


def _safe_stem(text: str, fallback: str = "article") -> str:
    raw = (text or "").strip() or fallback
    # 去掉路径非法字符
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", raw)
    cleaned = cleaned.strip(" ._")[:60] or fallback
    return cleaned


def _split_blocks(article: str) -> list[tuple[str, str]]:
    """粗分标题/段落。返回 [(kind, text)] kind in heading1|heading2|para。"""
    blocks: list[tuple[str, str]] = []
    lines = (article or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# "):
            blocks.append(("heading1", line[2:].strip()))
        elif line.startswith("## "):
            blocks.append(("heading2", line[3:].strip()))
        elif line.startswith("### "):
            blocks.append(("heading2", line[4:].strip()))
        elif re.match(r"^【.+】$", line) or re.match(r"^第[一二三四五六七八九十\d]+[章节部分]", line):
            blocks.append(("heading2", line))
        else:
            blocks.append(("para", line))
    if not blocks:
        blocks.append(("para", (article or "").strip() or "（空文稿）"))
    # 若全文无 heading，第一段当标题
    if blocks and all(k == "para" for k, _ in blocks):
        first = blocks[0][1]
        if len(first) <= 40:
            blocks[0] = ("heading1", first)
    return blocks


def _pick_chinese_font() -> str | None:
    candidates = [
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\msyh.ttf"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def export_docx(article: str, *, title: str, meta: dict | None = None) -> Path:
    from docx import Document
    from docx.enum.text import WD_LINE_SPACING
    from docx.shared import Pt

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = EXPORT_DIR / f"{_safe_stem(title)}_{stamp}.docx"

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(11)
    pf = style.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE

    for kind, text in _split_blocks(article):
        if kind == "heading1":
            doc.add_heading(text, level=1)
        elif kind == "heading2":
            doc.add_heading(text, level=2)
        else:
            doc.add_paragraph(text)

    doc.save(str(path))
    return path


def export_pdf(article: str, *, title: str, meta: dict | None = None) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = EXPORT_DIR / f"{_safe_stem(title)}_{stamp}.pdf"

    font_path = _pick_chinese_font()
    font_name = "Helvetica"
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont("GeoCN", font_path))
            font_name = "GeoCN"
        except Exception:
            # ttc 有时需 subfontIndex；失败则退回 Helvetica（中文会缺字）
            try:
                pdfmetrics.registerFont(TTFont("GeoCN", font_path, subfontIndex=0))
                font_name = "GeoCN"
            except Exception:
                font_name = "Helvetica"

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "GeoBody",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=11,
        leading=18,
        spaceAfter=10,
    )
    h1 = ParagraphStyle(
        "GeoH1",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=16,
        leading=22,
        spaceAfter=12,
        spaceBefore=6,
    )
    h2 = ParagraphStyle(
        "GeoH2",
        parent=styles["Heading2"],
        fontName=font_name,
        fontSize=13,
        leading=20,
        spaceAfter=8,
        spaceBefore=8,
    )

    def esc(t: str) -> str:
        return (
            (t or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=title or "GEO article",
    )
    story = []
    for kind, text in _split_blocks(article):
        if kind == "heading1":
            story.append(Paragraph(esc(text), h1))
        elif kind == "heading2":
            story.append(Paragraph(esc(text), h2))
        else:
            story.append(Paragraph(esc(text), body))
    doc.build(story)
    return path


def export_article_files(
    article: str,
    *,
    title: str,
    formats: list[str] | None = None,
    meta: dict | None = None,
) -> dict[str, str]:
    """导出文件，返回 {docx|pdf: 相对 data 的路径}。"""
    wanted = [x.lower().strip() for x in (formats or ["docx", "pdf"]) if x]
    if not wanted:
        wanted = ["docx", "pdf"]
    out: dict[str, str] = {}
    if "docx" in wanted or "doc" in wanted:
        p = export_docx(article, title=title, meta=meta)
        out["docx"] = str(p.relative_to(ROOT)).replace("\\", "/")
    if "pdf" in wanted:
        p = export_pdf(article, title=title, meta=meta)
        out["pdf"] = str(p.relative_to(ROOT)).replace("\\", "/")

    # 同时保存 markdown 到知识库 brand/articles/（知识库不在本机时跳过）
    try:
        kb_root = find_kb_root()
        if kb_root is None:
            return out
        articles_dir = kb_root / "brand" / "articles"
        articles_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        md_path = articles_dir / f"{_safe_stem(title)}_{stamp}.md"
        frontmatter = (
            "---\n"
            f"title: {title}\n"
            f"created: {datetime.now().strftime('%Y-%m-%d')}\n"
            f"updated: {datetime.now().strftime('%Y-%m-%d')}\n"
            "type: article\n"
            "zone: brand\n"
            "tags: [brand, article, generated]\n"
            "sources: [GEO工具生成]\n"
            "confidence: medium\n"
            "---\n\n"
        )
        md_path.write_text(frontmatter + article, encoding="utf-8")
    except Exception:
        pass  # 知识库保存失败不影响主流程

    return out
