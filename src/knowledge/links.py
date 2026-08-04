"""内部リンク検査。

全 HTML をパースして href/src を抽出し、/knowledge/ 配下の内部リンクが
出力 tree 内に実在するかを確認する。base_path の手書き `../` は使わない。
"""
from __future__ import annotations
import posixpath
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Sequence

BASE = "/knowledge/"


@dataclass(frozen=True)
class LinkReport:
    ok: bool
    broken: tuple[tuple[str, str], ...]  # (href, missing_file)


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []  # (href, attr_name)

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k in ("href", "src") and v:
                self.links.append((v, k))


def check_internal_links(root: Path, *, base_path: str = BASE) -> LinkReport:
    broken: list[tuple[str, str]] = []
    for html_path in sorted(root.rglob("*.html")):
        p = _LinkExtractor()
        p.feed(html_path.read_text(encoding="utf-8"))
        p.close()
        for href, _attr in p.links:
            # 絶対外部 URL は検査対象外
            if href.startswith(("http://", "https://", "mailto:", "javascript:", "data:")):
                continue
            if href.startswith("#"):
                continue
            # base_path 配下の内部リンクのみ解決
            if base_path and href.startswith(base_path):
                rel = href[len(base_path):]
            elif not base_path:
                rel = href
            else:
                continue
            rel = rel.split("#", 1)[0]
            if rel == "":
                rel = "index.html"
            if not rel.startswith("/"):
                # 相対リンク（現状は絶対 base_path 前提）は root 基準へ
                rel = "/" + rel
            target = root / rel.lstrip("/")
            if not target.exists():
                broken.append((href, rel))
    return LinkReport(ok=not broken, broken=tuple(broken))
