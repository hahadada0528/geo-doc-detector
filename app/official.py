"""官网自我判定：根据品牌名 / 别名 / 已知域名判断引用是否来自品牌方站点。"""
from __future__ import annotations

import re
from urllib.parse import urlparse


def _normalize_host(url_or_host: str) -> str:
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).hostname or ""
    return host.removeprefix("www.")


def _tokens(brand_name: str, aliases: list[str]) -> list[str]:
    tokens = []
    for item in [brand_name, *aliases]:
        s = (item or "").strip().lower()
        if not s:
            continue
        tokens.append(s)
        # 去掉常见公司后缀，便于匹配域名里的品牌词
        for suffix in ("有限公司", "股份有限公司", "集团", "科技", "官方", "官网"):
            if s.endswith(suffix) and len(s) > len(suffix) + 1:
                tokens.append(s[: -len(suffix)])
    # 去重保序
    seen = set()
    out = []
    for t in tokens:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def is_official_site(
    *,
    brand_name: str,
    aliases: list[str] | None = None,
    official_domains: list[str] | None = None,
    url: str | None = None,
    site_name: str | None = None,
    title: str | None = None,
) -> bool:
    """判定引用站点是否为品牌方官网。

    规则（任一命中即视为官网）：
    1. 域名命中用户配置的 official_domains
    2. 站点名 / 标题明确写「官网」且含品牌名
    3. 域名主体包含品牌名或别名（长度 >= 2）
    4. 站点名等于品牌名或「品牌+官网」类写法
    """
    aliases = aliases or []
    official_domains = official_domains or []
    host = _normalize_host(url or "")
    site = (site_name or "").strip().lower()
    title_l = (title or "").strip().lower()
    brand = (brand_name or "").strip().lower()
    tokens = _tokens(brand_name, aliases)

    for d in official_domains:
        od = _normalize_host(d)
        if od and (host == od or host.endswith("." + od)):
            return True

    # 「XX官网」类站点名
    if site and brand and brand in site and ("官网" in site or "官方" in site):
        return True
    if title_l and brand and brand in title_l and ("官网" in title_l or "官方网站" in title_l):
        return True

    # 站点名就是品牌名（常见于搜索结果的 site_name）
    if site and brand and (site == brand or site.replace(" ", "") == brand.replace(" ", "")):
        return True
    for alias in aliases:
        a = alias.strip().lower()
        if a and site == a:
            return True

    # 域名主体含品牌词（避免单字误伤）
    if host:
        label = host.split(".")[0]
        for t in tokens:
            if len(t) < 2:
                continue
            # 中文品牌：直接包含；英文：整段匹配或连字符边界
            if re.search(r"[\u4e00-\u9fff]", t):
                if t in host or t in label:
                    return True
            else:
                if label == t or label.startswith(t) or f"-{t}" in label or f"{t}-" in label:
                    return True
                if host.startswith(t + ".") or f".{t}." in f".{host}.":
                    return True

    return False


def display_site_name(
    *,
    brand_name: str,
    aliases: list[str] | None = None,
    official_domains: list[str] | None = None,
    url: str | None = None,
    site_name: str | None = None,
    title: str | None = None,
) -> tuple[str, bool]:
    """返回入库用的站点名。官网则固定为「官网」。"""
    official = is_official_site(
        brand_name=brand_name,
        aliases=aliases,
        official_domains=official_domains,
        url=url,
        site_name=site_name,
        title=title,
    )
    if official:
        return "官网", True

    name = (site_name or "").strip()
    if name:
        return name, False

    host = _normalize_host(url or "")
    if host:
        return host, False

    title_s = (title or "").strip()
    if title_s:
        return title_s[:40], False

    return "未知来源", False
