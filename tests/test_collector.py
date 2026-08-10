"""collector のテスト：feed パース、window フィルタ、proposed checkpoint。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Mapping

import pytest

from knowledge import collector, feeds
from knowledge.models import Checkpoint, SourceConfig, SourceCheckpoint

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>F</title>
  <entry>
    <id>urn:x:1</id>
    <title>Article One</title>
    <link rel="alternate" href="https://example.com/1"/>
    <published>2026-08-02T10:00:00Z</published>
  </entry>
  <entry>
    <id>urn:x:2</id>
    <title>Article Two</title>
    <link rel="alternate" href="https://example.com/2"/>
    <published>2026-08-03T00:00:00Z</published>
  </entry>
</feed>"""

RSS = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <guid>g-3</guid>
      <title>RSS Item</title>
      <link>https://example.com/3</link>
      <pubDate>Wed, 02 Aug 2026 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""


def _src(kind="atom", url="https://example.com/feed", required=True) -> SourceConfig:
    return SourceConfig(
        id="s1", kind=kind, url=url, allowed_hosts=("example.com",),
        priority=100, required=required,
    )


ARTICLE = b'<html><body><p>Long enough article body content for summarization and source_text extraction.</p></body></html>'


class FakeHttp:
    def __init__(self, bodies: Mapping[str, bytes]):
        self.bodies = bodies

    def get(self, url, *, allowed_hosts, headers=None):
        if url not in self.bodies:
            raise feeds.SafeHttpError(f"missing body for {url}")
        return feeds.HttpResult(200, {}, self.bodies[url], url)


def test_parse_atom_feed():
    http = FakeHttp({
        "https://example.com/feed": ATOM,
        "https://example.com/1": ARTICLE,
        "https://example.com/2": ARTICLE,
    })
    c = collector._fetch_feed(_src(), http,
                              collector.CollectionWindow("2026-08-01T00:00:00Z",
                                                         "2026-08-01T00:00:00Z",
                                                         "2026-08-03T01:00:00Z"),
                              retrieved_at="2026-08-03T01:00:00Z")
    # window 内（fetch_start 以降）の entry のみ
    assert len(c) == 2
    assert c[0].external_id == "urn:x:1"
    assert c[0].canonical_url == "https://example.com/1"


def test_parse_rss_feed():
    http = FakeHttp({
        "https://example.com/feed": RSS,
        "https://example.com/3": ARTICLE,
    })
    c = collector._fetch_feed(_src(kind="feed"), http,
                              collector.CollectionWindow("2026-08-01T00:00:00Z",
                                                         "2026-08-01T00:00:00Z",
                                                         "2026-08-03T01:00:00Z"),
                              retrieved_at="2026-08-03T01:00:00Z")
    assert len(c) == 1
    assert c[0].external_id == "g-3"
    assert c[0].canonical_url == "https://example.com/3"


def test_collect_all_updates_proposed_checkpoint():
    http = FakeHttp({
        "https://example.com/feed": ATOM,
        "https://example.com/1": ARTICLE,
        "https://example.com/2": ARTICLE,
    })
    cp = Checkpoint(1, "2026-08-01T00:00:00Z", {})
    res = collector.collect_all(
        (_src(),), cp, run_started_at="2026-08-03T01:00:00Z", http=http,
    )
    assert len(res.candidates) == 2
    # P0: collector は seen に追加しない（merge が検証後に追加）。watermark も進めない。
    assert res.proposed_checkpoint.last_success_at == "2026-08-01T00:00:00Z"
    s = res.proposed_checkpoint.sources["s1"]
    assert len(s.seen) == 0


def test_dtd_rejected():
    with pytest.raises(feeds.SafeHttpError):
        feeds.parse_feed(b'<!DOCTYPE foo><feed><item></item></feed>', _src(), retrieved_at="x")


def test_required_source_failure_does_not_advance_checkpoint():
    # required な source の body が無い → 失敗、proposed checkpoint は進まない
    http = FakeHttp({"https://example.com/feed": ATOM})
    cp = Checkpoint(1, "2026-08-01T00:00:00Z", {})
    src = _src(required=True, url="https://example.com/missing")
    res = collector.collect_all(
        (src,), cp, run_started_at="2026-08-03T01:00:00Z", http=http,
    )
    # 失敗 source は ok=False で stats に記録
    assert not res.source_stats[0].ok
    assert res.source_stats[0].error is not None
    # proposed checkpoint には失敗 source の seen が入らない（進めない）
    assert "s1" not in res.proposed_checkpoint.sources
    # candidates も出さない
    assert res.candidates == ()


def test_optional_source_failure_is_tolerated():
    # optional な source の失敗は candidates を出し、proposed checkpoint の seen も作らない
    http = FakeHttp({"https://example.com/feed": ATOM})
    cp = Checkpoint(1, "2026-08-01T00:00:00Z", {})
    src = _src(required=False, url="https://example.com/missing")
    res = collector.collect_all(
        (src,), cp, run_started_at="2026-08-03T01:00:00Z", http=http,
    )
    assert not res.source_stats[0].ok
    assert res.proposed_checkpoint.sources == {}


def test_html_index_nav_links_excluded_and_no_enrichment_in_fetch_feed():
    # _fetch_feed は index 解析と window フィルタのみ。nav 内リンクは候補にしない。本文 enrichment は collect_all で行う。
    index_body = (
        b'<html><nav><a href="/about">nav</a></nav>'
        b'<article><a href="/post/1">Post One</a><time>Aug 1, 2026</time></article>'
        b'<article><a href="/post/2">Post Two</a><time>Jul 30, 2026</time></article></html>'
    )
    http = FakeHttp({
        "https://example.com/": index_body,
    })
    src = _src(kind="html-index", url="https://example.com/")
    c = collector._fetch_feed(
        src, http,
        collector.CollectionWindow("2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", "2026-08-03T01:00:00Z"),
        retrieved_at="2026-08-03T01:00:00Z",
    )
    assert len(c) == 2
    assert all(x.canonical_url == "https://example.com/post/1" for x in c) is False
    urls = {x.canonical_url for x in c}
    assert urls == {"https://example.com/post/1", "https://example.com/post/2"}
    # _fetch_feed は enrichment しない（source_text は空のまま）
    assert all(x.source_text == "" for x in c)


def test_rss_excerpt_not_enriched_in_fetch_feed():
    # RSS description が短い候補は _fetch_feed では enrichment しない（source_text は元のまま）
    rss_body = (
        b'<rss version="2.0"><channel><item>'
        b'<guid>g1</guid><title>Post</title><link>https://example.com/a</link>'
        b'<description>short excerpt</description>'
        b'</item></channel></rss>'
    )
    http = FakeHttp({"https://example.com/feed": rss_body})
    src = _src(kind="feed", url="https://example.com/feed")
    c = collector._fetch_feed(
        src, http,
        collector.CollectionWindow("2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", "2026-08-03T01:00:00Z"),
        retrieved_at="2026-08-03T01:00:00Z",
    )
    assert len(c) == 1
    assert c[0].source_text == "short excerpt"


def test_collect_all_enriches_selected_and_defers_rest():
    # collect_all は summary_quota までを selected とし、それらだけ本文 enrichment する。
    # それ以外は deferred になり、watermark は進まない。
    index_body = (
        b'<html>'
        b'<article><a href="/post/1">Post One</a><time>Aug 1, 2026</time></article>'
        b'<article><a href="/post/2">Post Two</a><time>Jul 30, 2026</time></article>'
        b'<article><a href="/post/3">Post Three</a><time>Jul 29, 2026</time></article>'
        b'</html>'
    )
    article_body = (
        b'<html><body><p>This is a long enough article body for summarization that gets fetched into source_text.</p></body></html>'
    )
    http = FakeHttp({
        "https://example.com/": index_body,
        "https://example.com/post/1": article_body,
        "https://example.com/post/2": article_body,
        "https://example.com/post/3": article_body,
    })
    cp = Checkpoint(1, "2026-08-01T00:00:00Z", {})
    res = collector.collect_all(
        (_src(kind="html-index", url="https://example.com/"),),
        cp, run_started_at="2026-08-03T01:00:00Z", http=http, summary_quota=2,
    )
    # selected 2 件だけ本文が埋まる
    assert len(res.candidates) == 2
    assert all(c.source_text for c in res.candidates)
    # deferred 1 件は watermark を進めない（last_success_at 維持）
    assert len(res.deferred_candidate_ids) == 1
    assert res.proposed_checkpoint.last_success_at == "2026-08-01T00:00:00Z"


def test_utc_parse_full_month_name():
    # Apple の news ページは "August 5, 2026" のような完全月名を使う（<time> ではなく article-date）
    dt = collector._utc_parse("August 5, 2026")
    assert dt.isoformat().startswith("2026-08-05")
    dt2 = collector._utc_parse("Jul 24, 2026")
    assert dt2.isoformat().startswith("2026-07-24")


def test_html_index_apple_article_date_extraction():
    # Apple 形式: 日付は <p class="article-date"> にあり <time> は無い。ブロック単位で抽出し、
    # window フィルタが古い記事を除外する。
    index_body = (
        b'<html><nav><a href="/about">nav</a></nav>'
        b'<article><a href="/news/?id=1">One</a><p class="lighter article-date">August 5, 2026</p></article>'
        b'<article><a href="/news/?id=2">Two</a><p class="lighter article-date">July 20, 2026</p></article>'
        b'<article><a href="/news/?id=3">Three</a><p class="lighter article-date">June 23, 2026</p></article>'
        b'</html>'
    )
    http = FakeHttp({"https://example.com/": index_body})
    src = _src(kind="html-index", url="https://example.com/")
    # window: 2026-07-11 〜 2026-08-10 → August 5 と July 20 のみ通過、June 23 は除外
    c = collector._fetch_feed(
        src, http,
        collector.CollectionWindow("2026-07-11T00:00:00Z", "2026-07-11T00:00:00Z", "2026-08-10T00:00:00Z"),
        retrieved_at="2026-08-10T00:00:00Z",
    )
    assert len(c) == 2
    urls = {x.canonical_url for x in c}
    assert urls == {"https://example.com/news/?id=1", "https://example.com/news/?id=2"}
    # 日付が正しく抽出されている（空でない）
    assert all(x.published_at for x in c)
    by_url = {x.canonical_url: x.published_at for x in c}
    assert by_url["https://example.com/news/?id=1"] == "August 5, 2026"


def test_html_index_releases_data_href_link():
    # Apple releases は記事 URL を share ボタンの data-href に持つ（<a href> は /download/ 等）。
    # /news/ を含むリンクを優先して正しい記事 URL を選ぶ。
    index_body = (
        b'<html>'
        b'<article>'
        b'<a href="/download/">iOS 26.6</a>'
        b'<button data-href="https://developer.apple.com/news/releases/?id=07272026a"></button>'
        b'<p class="lighter article-date">July 27, 2026</p>'
        b'</article>'
        b'<article>'
        b'<a href="/download/">iOS 26.5</a>'
        b'<button data-href="https://developer.apple.com/news/releases/?id=07272026b"></button>'
        b'<p class="lighter article-date">July 27, 2026</p>'
        b'</article>'
        b'</html>'
    )
    http = FakeHttp({"https://developer.apple.com/news/releases/": index_body})
    src = _src(kind="html-index", url="https://developer.apple.com/news/releases/")
    c = collector._fetch_feed(
        src, http,
        collector.CollectionWindow("2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", "2026-08-10T00:00:00Z"),
        retrieved_at="2026-08-10T00:00:00Z",
    )
    assert len(c) == 2
    urls = {x.canonical_url for x in c}
    assert urls == {"https://developer.apple.com/news/releases/?id=07272026a",
                    "https://developer.apple.com/news/releases/?id=07272026b"}
    assert all(x.published_at == "July 27, 2026" for x in c)


def test_html_index_fallback_filters_nav_and_pairs_dates():
    # <article> が単一ラッパーのページ（Anthropic 等）は index 順対応にフォールバック。
    # root / nav リンクを除外し、記事リンクと <time> 日付を順対応で正しく対応付ける。
    index_body = (
        b'<html>'
        b'<a href="/">Home</a><a href="/research">Research</a>'
        b'<a href="/news/claude-opus-5">Opus 5</a><time>Jul 24, 2026</time>'
        b'<a href="/news/hard-questions">Hard Q</a><time>Jul 9, 2026</time>'
        b'</html>'
    )
    http = FakeHttp({"https://www.anthropic.com/news": index_body})
    src = _src(kind="html-index", url="https://www.anthropic.com/news")
    c = collector._fetch_feed(
        src, http,
        collector.CollectionWindow("2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", "2026-08-10T00:00:00Z"),
        retrieved_at="2026-08-10T00:00:00Z",
    )
    assert len(c) == 2
    urls = {x.canonical_url for x in c}
    assert urls == {"https://www.anthropic.com/news/claude-opus-5",
                    "https://www.anthropic.com/news/hard-questions"}
    by_url = {x.canonical_url: x.published_at for x in c}
    assert by_url["https://www.anthropic.com/news/claude-opus-5"] == "Jul 24, 2026"
    assert by_url["https://www.anthropic.com/news/hard-questions"] == "Jul 9, 2026"
