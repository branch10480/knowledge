"""URL 正規化と永続 ID / 重複判定。

URL 正規化は scheme/host 小文字化、default port 除去、path の dot segment 解決、
tracking parameter 除去を行う。意味が変わり得る query parameter の並べ替え・削除は
source adapter の明示設定なしには行わない。
"""
from __future__ import annotations
import hashlib
import re
from typing import Collection, Mapping, Sequence

from .models import Candidate, Checkpoint, EntriesDocument, Entry, SourceConfig

TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid",
})


def _sha256(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


def normalize_canonical_url(raw_url: str, *, allowed_hosts: Collection[str]) -> str:
    if not raw_url.startswith("https://"):
        raise ValueError(f"non-HTTPS url: {raw_url!r}")
    m = re.match(r"^https://([^/?#]+)(/[^?#]*)?(\?[^#]*)?(#.*)?$", raw_url)
    if not m:
        raise ValueError(f"malformed url: {raw_url!r}")
    authority = m.group(1).lower()
    path = m.group(2) or ""
    query = m.group(3) or ""
    if "@" in authority:
        raise ValueError(f"userinfo in url: {raw_url!r}")
    if authority.startswith("localhost"):
        raise ValueError(f"forbidden host: {authority!r}")
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", authority):
        raise ValueError(f"IP literal host: {authority!r}")
    # port 除去（default port のみ）
    authority = re.sub(r":(80|443)$", "", authority)
    # dot segment 解決
    segs = path.split("/")
    out: list[str] = []
    for seg in segs:
        if seg == "." or seg == "":
            continue
        if seg == "..":
            if out:
                out.pop()
        else:
            out.append(seg)
    path = "/" + "/".join(out) if out else "/"
    # host 照合
    host = authority.split(":")[0]
    if not any(h == host for h in allowed_hosts):
        raise ValueError(f"host not in allowlist: {host!r}")
    # tracking param 除去
    keep: list[str] = []
    if query:
        for kv in query[1:].split("&"):
            if "=" in kv:
                k, _ = kv.split("=", 1)
                if k in TRACKING_PARAMS:
                    continue
            keep.append(kv)
        query = "&".join(keep)
    return f"https://{authority}{path}" + (f"?{query}" if query else "")


def stable_external_id(item: Mapping, canonical_url: str) -> str:
    """優先順に feed GUID / GitHub node id / 正規化 canonical URL を使う。"""
    for key in ("guid", "id", "node_id"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return canonical_url


def make_candidate_id(source_id: str, external_id: str) -> str:
    return _sha256(f"{source_id}\n{external_id}")


def make_entry_id(candidate: Candidate) -> str:
    # _sha256 は "sha256:<hex>" を返す。hex 部分のみ（[7:31]）を 24 文字で使う
    return "kn_" + _sha256(f"{candidate.source_id}\n{candidate.external_id}")[7:31]


def is_known(candidate: Candidate, entries: EntriesDocument, checkpoint: Checkpoint) -> bool:
    """既知判定は、既存 entries と checkpoint.seen の両方を参照する。"""
    cid = make_entry_id(candidate)
    if any(e.id == cid for e in entries.entries):
        return True
    src = checkpoint.sources.get(candidate.source_id)
    if src is None:
        return False
    ext_hash = _sha256(candidate.external_id)
    for seen in src.seen:
        if seen.get("external_id_hash") == ext_hash:
            return True
        if seen.get("canonical_url_hash") == _sha256(candidate.canonical_url):
            return True
    return False


def entry_url(entry: Entry, *, base_path: str = "/knowledge/") -> str:
    return f"{base_path}entry/{entry.id}.html"


def archive_url(year_month: str, *, base_path: str = "/knowledge/") -> str:
    return f"{base_path}archive/{year_month}.html"
