"""安全な HTTP クライアントと feed パーサ。

- HTTPS のみ、GET/HEAD、allowlist host。redirect は最大 3 回、各 hop を再検証
- 応答は展開後 byte 数も制限
- XML は外部 entity/DTD を無効化した安全解析
"""
from __future__ import annotations
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Mapping, Sequence, Tuple

from .models import Candidate, SourceConfig

_MAX_REDIRECTS = 3


class SafeHttpError(Exception):
    pass


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str


class SafeHttpClient:
    def __init__(self, *, timeout_seconds: int = 20, max_bytes: int = 2 * 1024 * 1024):
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    def get(self, url: str, *, allowed_hosts: Sequence[str], headers: Mapping[str, str] | None = None) -> HttpResult:
        seen: set[str] = set()
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            self._validate_url(current, allowed_hosts)
            if current in seen:
                raise SafeHttpError("redirect loop")
            seen.add(current)
            hdrs = dict(headers or {})
            hdrs.setdefault("User-Agent", "KnowledgeCollector/2.0 (+https://github.com/branch10480/knowledge)")
            req = urllib.request.Request(current, headers=hdrs, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    status = resp.status
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    body = resp.read(self.max_bytes + 1)
                    if len(body) > self.max_bytes:
                        raise SafeHttpError(f"response too large > {self.max_bytes}")
                    final_url = resp.geturl()
                    loc = headers.get("location")
                    if status in (301, 302, 303, 307, 308) and loc:
                        current = urllib.parse.urljoin(current, loc)
                        continue
                    return HttpResult(status, headers, body, final_url)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location")
                    if loc:
                        current = urllib.parse.urljoin(current, loc)
                        continue
                raise SafeHttpError(f"http error {e.code}")
            except urllib.error.URLError as e:
                raise SafeHttpError(str(e))
        raise SafeHttpError("too many redirects")

    @staticmethod
    def _validate_url(url: str, allowed_hosts: Sequence[str]) -> None:
        if not url.startswith("https://"):
            raise SafeHttpError(f"non-HTTPS url: {url!r}")
        m = re.match(r"^https://([^/?#]+)", url)
        if not m:
            raise SafeHttpError(f"malformed url: {url!r}")
        host = m.group(1).lower().split(":")[0]
        if host not in allowed_hosts:
            raise SafeHttpError(f"host not in allowlist: {host!r}")


def _safe_parse_xml(data: bytes) -> ET.Element:
    # ElementTree は外部 entity / DTD を解決しない（安全）。
    # 明示的に外部 entity を拒否するため、DOCTYPE を含む入力を拒否する。
    text = data.decode("utf-8", errors="replace")
    if "<!DOCTYPE" in text or "<!ENTITY" in text or "<!ELEMENT" in text:
        raise SafeHttpError("xml with DTD/entity rejected")
    root = ET.fromstring(text)
    return root


def parse_feed(
    payload: bytes, source: SourceConfig, *, retrieved_at: str
) -> tuple[Candidate, ...]:
    """RSS/Atom を安全にパースして候補を返す。"""
    root = _safe_parse_xml(payload)
    root_tag = root.tag
    source_ns = ""
    if root_tag.startswith("{"):
        source_ns = root_tag[1:root_tag.index("}")]
    items: list[ET.Element] = []
    # Atom: entry / RSS: item
    items.extend(root.findall(".//{http://www.w3.org/2005/Atom}entry"))
    items.extend(root.findall(".//item"))
    if not items:
        # 名前空間なしの entry / item も試す
        items.extend(root.findall(".//entry"))
        items.extend(root.findall(".//item"))

    def _text(el: ET.Element, *tags: str) -> str:
        for tag in tags:
            cand = el.find(f".//{{{source_ns}}}{tag}") if source_ns else el.find(f".//{tag}")
            if cand is not None and cand.text:
                return cand.text.strip()
        return ""

    def _link(el: ET.Element) -> str:
        if source_ns:
            a = el.find(f".//{{{source_ns}}}link[@rel='alternate']")
            if a is None:
                a = el.find(f".//{{{source_ns}}}link")
            if a is not None and a.get("href"):
                return a.get("href").strip()
            return ""
        lk = el.find(".//link")
        return lk.text.strip() if lk is not None and lk.text else ""

    out: list[Candidate] = []
    for item in items:
        guid = _text(item, "guid", "id")
        title = _text(item, "title")
        link = _link(item)
        pub = _text(item, "published", "pubDate", "updated")
        body = _text(item, "description", "content", "summary")
        if not title and not link:
            continue
        canonical = link or guid
        external = guid or canonical
        out.append(Candidate(
            candidate_id="",
            source_id=source.id,
            source_kind=source.kind,
            external_id=external,
            canonical_url=canonical,
            title=title,
            published_at=pub,
            updated_at=pub,
            retrieved_at=retrieved_at,
            source_text=body,
            priority=source.priority,
        ))
    return tuple(out)
