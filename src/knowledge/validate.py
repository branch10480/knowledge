"""検証・sanitize。

- jsonschema で entry/entries/checkpoint を検証
- plain text の sanitize（制御文字、双方向制御、非文字、HTML タグを拒否、NFC 正規化）
- HTTPS のみ、userinfo/fragment/localhost/IP 禁止
- factual gate：claims の evidence が source_text に正規化一致するか
"""
from __future__ import annotations
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import FormatChecker, validate as jsonschema_validate

from .models import Candidate, EntriesDocument, Entry, SourceConfig

_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"

# 双方向制御文字・非文字コードポイント
_BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069\u061c]")
_NONCHAR = re.compile(r"[\ufdd0-\ufdef\ufffe\uffff]")
_NUL = re.compile(r"\x00")
_HTML_TAG = re.compile(r"<[^>]*>|</[^>]*>|<[A-Za-z/][^>]*>")
_SCRIPT = re.compile(r"<script|</script|javascript:|on\w+\s*=|style\s*=", re.IGNORECASE)


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    errors: tuple[str, ...]

    def raise_if_bad(self) -> None:
        if not self.ok:
            raise ValidationError("; ".join(self.errors))


class ValidationError(Exception):
    pass


def _load_schema(name: str) -> dict:
    p = _SCHEMA_DIR / f"{name}.schema.json"
    if not p.exists():
        raise ValidationError(f"missing schema: {name}")
    return json.loads(p.read_text(encoding="utf-8"))


def validate_entries_document(document: Mapping[str, Any]) -> ValidationReport:
    errs: list[str] = []
    try:
        jsonschema_validate(document, _load_schema("entries"), format_checker=FormatChecker())
    except Exception as e:  # jsonschema.ValidationError
        errs.append(str(e))
    # 危険 URL / 不正日時は Schema の format では通るため、追加で厳密検証
    for e in document.get("entries", []) or []:
        eid = e.get("id", "?")
        cu = e.get("canonical_url", "")
        pa = e.get("published_at", "")
        if cu:
            try:
                validate_url(cu, allowed_hosts=None)
            except ValidationError as ve:
                errs.append(f"{eid}: {ve}")
        if pa and not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", pa):
            errs.append(f"{eid}: bad published_at {pa!r}")
    return ValidationReport(ok=not errs, errors=tuple(errs))


def validate_checkpoint(checkpoint: Mapping[str, Any]) -> ValidationReport:
    errs: list[str] = []
    try:
        jsonschema_validate(checkpoint, _load_schema("checkpoint"), format_checker=FormatChecker())
    except Exception as e:
        errs.append(str(e))
    return ValidationReport(ok=not errs, errors=tuple(errs))


def sanitize_plain_text(value: str, *, max_chars: int) -> str:
    v = unicodedata.normalize("NFC", value)
    v = _NUL.sub("", v)
    v = _BIDI.sub("", v)
    v = _NONCHAR.sub("", v)
    if _HTML_TAG.search(v) or _SCRIPT.search(v):
        raise ValidationError("html/script-like content rejected in plain text")
    v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v)
    v = v.replace("\r", " ").replace("\n", " ").strip()
    if len(v) > max_chars:
        raise ValidationError(f"text too long ({len(v)} > {max_chars})")
    return v


def validate_url(url: str, *, allowed_hosts: Sequence[str] | None = None) -> None:
    if not url.startswith("https://"):
        raise ValidationError(f"non-HTTPS url")
    if " " in url or "@" in url:
        raise ValidationError(f"forbidden userinfo/space in url")
    m = re.match(r"^https://([^/?#]+)(/[^?#]*)?(\?[^#]*)?", url)
    if not m:
        raise ValidationError(f"malformed url")
    host = m.group(1).lower().split(":")[0]
    if host.startswith("localhost"):
        raise ValidationError(f"forbidden host")
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
        raise ValidationError(f"IP literal host")
    if allowed_hosts is not None:
        if host not in allowed_hosts:
            raise ValidationError(f"host not in allowlist")


def validate_entry(entry: Entry, *, allowed_hosts: Sequence[str] | None = None) -> None:
    validate_url(entry.canonical_url, allowed_hosts=allowed_hosts)
    for f in ("title", "summary"):
        v = getattr(entry, f)
        if _HTML_TAG.search(v) or _SCRIPT.search(v):
            raise ValidationError(f"{f} contains html/script-like content")
    if not re.match(r"^kn_[A-Za-z0-9]+$", entry.id):
        raise ValidationError(f"bad entry id: {entry.id!r}")
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", entry.published_at):
        raise ValidationError(f"bad published_at: {entry.published_at!r}")
    if not re.match(r"^sha256:[a-f0-9]{64}$", entry.source_digest):
        raise ValidationError(f"bad source_digest")


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str = ""


def factual_source_gate(summary: Mapping[str, Any], candidate: Candidate) -> GateResult:
    """claims の evidence が candidate.source_text に正規化一致するかを確認する。"""
    claims = summary.get("claims", [])
    if not isinstance(claims, list):
        return GateResult(False, "claims must be a list")
    text_norm = re.sub(r"\s+", " ", candidate.source_text)
    for claim in claims:
        if not isinstance(claim, dict):
            return GateResult(False, "claim not an object")
        ev = claim.get("evidence_quotes", [])
        if not isinstance(ev, list) or not ev:
            return GateResult(False, "claim has no evidence_quotes")
        for q in ev:
            q_norm = re.sub(r"\s+", " ", str(q)).strip()
            if not q_norm:
                return GateResult(False, "empty evidence quote")
            if q_norm not in text_norm:
                return GateResult(False, "evidence quote not found in source_text")
    if summary.get("insufficient_evidence"):
        return GateResult(False, "insufficient_evidence")
    return GateResult(True)
