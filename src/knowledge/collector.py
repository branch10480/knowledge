"""決定的 collector（LLM 不使用）。

収集窓 (previous_success_at - lookback, T0] を論理対象とし、ETag/GUID/canonical URL/
GitHub ID で新規判定する。checkpoint は提案（proposed）として返し、本番 commit は
merge 成功後にのみ行う（shadow run 可能）。
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Mapping, Sequence

from .feeds import SafeHttpClient, parse_feed, SafeHttpError
from .github_api import collect_github_releases
from .identity import make_candidate_id, make_entry_id
from .models import Checkpoint, SourceCheckpoint, SourceConfig, Candidate


def _utc_parse(s: str) -> datetime:
    s = s.strip()
    if not s:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    s = s.replace("Z", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return datetime.fromisoformat(s + "T00:00:00+00:00")
    # 'Mon D, YYYY'（例: Jul 24, 2026 / August 5, 2026）形式
    m = re.match(r"^([A-Za-z]{3,9}) (\d{1,2}), (\d{4})$", s)
    if m:
        import calendar
        try:
            month = {v: i for i, v in enumerate(calendar.month_abbr)}
            month.update({v: i for i, v in enumerate(calendar.month_name)})
            mm = month.get(m.group(1).title())
            if mm is not None:
                return datetime(int(m.group(3)), mm, int(m.group(2)), tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        from email.utils import parsedate_to_datetime
        try:
            dt = parsedate_to_datetime(s)
        except Exception:
            return datetime(1970, 1, 1, tzinfo=timezone.utc)
        if dt is None:
            return datetime(1970, 1, 1, tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)


def _utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CollectionWindow:
    logical_start: str
    fetch_start: str
    end: str


@dataclass(frozen=True)
class SourceStat:
    source_id: str
    fetched: int
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class CollectionResult:
    candidates: tuple[Candidate, ...]
    proposed_checkpoint: Checkpoint
    source_stats: tuple[SourceStat, ...]
    selected_candidate_ids: tuple[str, ...] = ()
    deferred_candidate_ids: tuple[str, ...] = ()


def _sha256(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


def _with_cid(c: Candidate) -> Candidate:
    """candidate_id が空なら永続 ID を埋める（要約出力との照合に必要）。"""
    if c.candidate_id:
        return c
    import dataclasses
    return dataclasses.replace(c, candidate_id=make_candidate_id(c.source_id, c.external_id))


def _collect_html_index(
    payload: bytes, source: SourceConfig, *, retrieved_at: str
) -> tuple[Candidate, ...]:
    """html-index（apple_releases 等）から記事リンクと日付を抽出する汎用 adapter。

    Apple の news/releases ページは日付を <p class="article-date"> に持ち、<time> は使わない。
    Anthropic 等は <time> を使う。複数の <article> ブロックがあるページはブロック単位で
    リンクと日付を対応付け、ブロックが無い（または単一ラッパーの）ページは従来の index 順
    対応にフォールバックする。
    """
    text = payload.decode("utf-8", errors="replace")

    class _Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.blocks: list[dict] = []   # 確定済み article ブロック: {"links","date"}
            self._cur: dict | None = None  # 現在の article ブロック
            self._skip_nav = 0
            self._in_time = False
            self._time_buf: list[str] = []
            self._date_depth = 0
            self._date_buf: list[str] = []
            # フォールバック（<article> 無し/単一ラッパー）用のグローバル収集
            self.fb_links: list[str] = []
            self.fb_dates: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag == "nav":
                self._skip_nav += 1
                return
            if tag == "article":
                self._cur = {"links": [], "date": ""}
                return
            if self._skip_nav:
                return
            if tag == "a":
                href = None
                for k, v in attrs:
                    if k == "href" and v and v.startswith(("https://", "/")):
                        href = v
                        break
                if href:
                    self.fb_links.append(href)
                    if self._cur is not None:
                        self._cur["links"].append(href)
            else:
                # Apple releases 等は記事 URL を share ボタンの data-href に持つ
                dhref = None
                for k, v in attrs:
                    if k == "data-href" and v and v.startswith(("https://", "/")):
                        dhref = v
                        break
                if dhref:
                    self.fb_links.append(dhref)
                    if self._cur is not None:
                        self._cur["links"].append(dhref)
            if tag == "time":
                self._in_time = True
                self._time_buf = []
            else:
                cls = ""
                for k, v in attrs:
                    if k == "class":
                        cls = v
                        break
                if cls and "article-date" in cls:
                    self._date_depth += 1
                    self._date_buf = []

        def handle_endtag(self, tag):
            if tag == "nav" and self._skip_nav > 0:
                self._skip_nav -= 1
                return
            if tag == "article" and self._cur is not None:
                self.blocks.append(self._cur)
                self._cur = None
                return
            if self._skip_nav:
                return
            if tag == "time" and self._in_time:
                self._in_time = False
                d = "".join(self._time_buf).strip()
                self.fb_dates.append(d)
                if self._cur is not None and not self._cur["date"]:
                    self._cur["date"] = d
            elif self._date_depth > 0:
                self._date_depth -= 1
                if self._date_depth == 0:
                    d = "".join(self._date_buf).strip()
                    self.fb_dates.append(d)
                    if self._cur is not None and not self._cur["date"]:
                        self._cur["date"] = d

        def handle_data(self, data):
            if self._in_time:
                self._time_buf.append(data)
            elif self._date_depth > 0:
                self._date_buf.append(data)

    p = _Parser()
    p.feed(text)
    p.close()

    from urllib.parse import urljoin
    source_url = source.url or ""
    base = source_url if source_url.startswith("https://") else "https://" + (source.allowed_hosts[0] if source.allowed_hosts else "")

    def _pick_link(links: list[str]) -> str:
        # 記事リンクらしいもの（/news/ を含む）を優先。無ければ最初のリンク。
        for l in links:
            if "/news/" in l:
                return l
        return links[0] if links else ""

    out: list[Candidate] = []
    # 1) 複数の <article> ブロックがあるページ（Apple 等）はブロック単位で対応付け
    if len(p.blocks) >= 2:
        for b in p.blocks[: source.max_items_per_source]:
            link = _pick_link(b.get("links", []))
            if not link:
                continue
            u = urljoin(base, link)
            out.append(Candidate(
                candidate_id="", source_id=source.id, source_kind=source.kind,
                external_id=u, canonical_url=u, title="",
                published_at=b.get("date", ""), updated_at=b.get("date", ""),
                retrieved_at=retrieved_at, priority=source.priority,
            ))
    # 2) フォールバック: <article> 無し/単一ラッパーのページ（Anthropic 等）は index 順対応
    else:
        # 記事リンクらしいもの（/news/ 等のコンテンツパス）だけに絞る（root / nav を除外）
        article_hrefs = [h for h in p.fb_links
                         if h.startswith("/") and re.search(r"/(news|features|blog|posts|articles|releases)/", h)]
        abs_hrefs = [urljoin(base, h) for h in article_hrefs]
        for i, u in enumerate(abs_hrefs[: source.max_items_per_source]):
            date = p.fb_dates[i] if i < len(p.fb_dates) else ""
            out.append(Candidate(
                candidate_id="", source_id=source.id, source_kind=source.kind,
                external_id=u, canonical_url=u, title="",
                published_at=date, updated_at=date, retrieved_at=retrieved_at,
                priority=source.priority,
            ))
    return tuple(out)


def _extract_article_text(payload: bytes) -> str:
    """HTML から script/style/nav/iframe を除外し、本文テキストを抽出する。"""
    text = payload.decode("utf-8", errors="replace")

    class _T(HTMLParser):
        def __init__(self):
            super().__init__()
            self.skip = 0
            self.parts: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "nav", "iframe", "svg"):
                self.skip += 1
            if tag in ("p", "li", "h1", "h2", "h3", "div", "br", "pre", "blockquote", "article"):
                self.parts.append(" ")

        def handle_endtag(self, tag):
            if tag in ("script", "style", "nav", "iframe", "svg") and self.skip > 0:
                self.skip -= 1
            if tag in ("p", "li", "h1", "h2", "h3", "div", "pre", "blockquote", "article"):
                self.parts.append(" ")

        def handle_data(self, data):
            if self.skip == 0:
                self.parts.append(data)

    p = _T()
    p.feed(text)
    p.close()
    out = re.sub(r"\s+", " ", "".join(p.parts)).strip()
    return out


def _fetch_feed(source: SourceConfig, http: SafeHttpClient, window: CollectionWindow, retrieved_at: str) -> list[Candidate]:
    res = http.get(source.url, allowed_hosts=source.allowed_hosts)
    if source.kind == "html-index":
        cands = _collect_html_index(res.body, source, retrieved_at=retrieved_at)
    else:
        cands = parse_feed(res.body, source, retrieved_at=retrieved_at)
    lo = _utc_parse(window.fetch_start)
    hi = _utc_parse(window.end)
    kept = []
    for c in cands:
        if c.published_at:
            t = _utc_parse(c.published_at)
            if lo <= t <= hi:
                kept.append(c)
        else:
            # 日付不明は要約段階で判定（insufficient_evidence）／enrichment で判定
            kept.append(c)

    # ここでは index/feed 解析と window フィルタのみ。本文 enrichment は collect_all で
    # run 全体の要約枠を確定した後に行う（P0: 全文取得を要約対象確定前にしない）
    return kept


def collect_all(
    sources: Sequence[SourceConfig],
    checkpoint: Checkpoint,
    *,
    run_started_at: str,
    http: SafeHttpClient,
    github_token: str | None = None,
    summary_quota: int | None = None,
) -> CollectionResult:
    prev = checkpoint.last_success_at
    prev_dt = _utc_parse(prev)
    end_dt = _utc_parse(run_started_at)
    # checkpoint 未設定（bootstrap）は直近 bootstrap_lookback_days だけを対象にする
    if prev_dt.year <= 1970:
        bootstrap_days = max(min(s.bootstrap_lookback_days for s in sources) or 30, 1)
        from datetime import timedelta
        fetch_start = _utc_str(end_dt - timedelta(days=bootstrap_days))
    else:
        lookback = max(min(s.lookback_hours for s in sources) or 72, 1)
        from datetime import timedelta
        fetch_start = _utc_str(prev_dt - timedelta(hours=lookback))
    window = CollectionWindow(
        logical_start=prev,
        fetch_start=fetch_start,
        end=run_started_at,
    )
    all_cands: list[Candidate] = []
    stats: list[SourceStat] = []
    proposed: dict[str, SourceCheckpoint] = dict(checkpoint.sources)

    for src in sources:
        st = proposed.get(src.id, SourceCheckpoint())
        try:
            if src.kind in ("atom", "feed", "html-index"):
                cands = _fetch_feed(src, http, window, retrieved_at=_utc_str(datetime.now(timezone.utc)))
                sel = cands[: src.max_items_per_source]
                # collector 段階では既知候補（checkpoint.seen）を除外し、新規候補だけ保持
                known_ext = {s.get("external_id_hash") for s in st.seen}
                known_canon = {s.get("canonical_url_hash") for s in st.seen}
                fresh = []
                for c in sel:
                    pair = (_sha256(c.external_id), _sha256(c.canonical_url))
                    if pair[0] in known_ext or pair[1] in known_canon:
                        continue
                    fresh.append(c)
                all_cands.extend(_with_cid(c) for c in fresh)
                # collector 段階では seen に追加しない（要約・検証完了後に merge が追加する）
                proposed[src.id] = SourceCheckpoint(etag=st.etag, last_modified=st.last_modified,
                                                    last_commit_sha=st.last_commit_sha, seen=st.seen)
                stats.append(SourceStat(src.id, len(fresh), True))
            elif src.kind == "github-releases":
                res = collect_github_releases(src, st, token=github_token, timeout=src.timeout_seconds)
                sel = res.candidates[: src.max_items_per_source]
                known_ext = {s.get("external_id_hash") for s in st.seen}
                known_canon = {s.get("canonical_url_hash") for s in st.seen}
                fresh = []
                for c in sel:
                    pair = (_sha256(c.external_id), _sha256(c.canonical_url))
                    if pair[0] in known_ext or pair[1] in known_canon:
                        continue
                    fresh.append(c)
                all_cands.extend(_with_cid(c) for c in fresh)
                proposed[src.id] = SourceCheckpoint(etag=res.new_etag or st.etag,
                                                    last_modified=st.last_modified,
                                                    last_commit_sha=res.new_last_commit_sha or st.last_commit_sha,
                                                    seen=st.seen)
                stats.append(SourceStat(src.id, len(fresh), res.ok, res.error))
            else:
                raise SafeHttpError(f"unknown source kind: {src.kind}")
        except SafeHttpError as e:
            stats.append(SourceStat(src.id, 0, False, str(e)))

    # 安定 sort：priority DESC, published_at ASC, candidate_id ASC
    all_cands.sort(key=lambda c: (-c.priority, c.published_at, make_candidate_id(c.source_id, c.external_id)))

    # run 全体の要約枠を確定してから、その候補だけ本文 enrichment する（P0）
    quota = summary_quota if summary_quota is not None else max((s.max_enrichment_items for s in sources), default=40)
    selected = all_cands[:quota]
    deferred = all_cands[quota:]
    selected_ids = tuple(make_candidate_id(c.source_id, c.external_id) for c in selected)
    deferred_ids = tuple(make_candidate_id(c.source_id, c.external_id) for c in deferred)

    # selected 候補の本文を取得（html-index / RSS excerpt）
    import dataclasses
    enriched: list[Candidate] = []
    for c in selected:
        src = next((s for s in sources if s.id == c.source_id), None)
        if src is None:
            enriched.append(c)
            continue
        needs_fetch = src.kind == "html-index" or len(c.source_text.strip()) < 50
        if needs_fetch and not c.canonical_url.startswith("https://"):
            enriched.append(c)
            continue
        if needs_fetch:
            try:
                ares = http.get(c.canonical_url, allowed_hosts=src.allowed_hosts)
                body = _extract_article_text(ares.body)
                if len(body.encode("utf-8")) > src.max_enrichment_bytes:
                    body = body.encode("utf-8")[: src.max_enrichment_bytes].decode("utf-8", errors="replace")
                if len(body.strip()) < 50:
                    # 本文不足は成功扱いにしない → deferred 扱い
                    deferred_ids += (make_candidate_id(c.source_id, c.external_id),)
                    continue
            except SafeHttpError:
                deferred_ids += (make_candidate_id(c.source_id, c.external_id),)
                continue
        else:
            body = c.source_text
        c2 = dataclasses.replace(
            c, source_text=body,
            source_digest="sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
        enriched.append(c2)
    selected_ids = tuple(make_candidate_id(c.source_id, c.external_id) for c in enriched)

    # collector 段階では seen に追加しないため、watermark は進めない（常に checkpoint を維持）。
    # watermark を進めるのは merge（検証済み candidate を seen に追加した後、deferred が無い場合のみ）。
    watermark = checkpoint.last_success_at
    proposed_cp = Checkpoint(
        schema_version=1, last_success_at=watermark, sources=proposed,
    )
    return CollectionResult(
        candidates=tuple(enriched),
        proposed_checkpoint=proposed_cp,
        source_stats=tuple(stats),
        selected_candidate_ids=selected_ids,
        deferred_candidate_ids=deferred_ids,
    )
