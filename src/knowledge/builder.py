"""安全なサイト生成。

- Jinja2 を autoescape=True + StrictUndefined で使い、URL は helper 経由のみ
- 一時ディレクトリに clean build し、QA 後に output_dir を原子的に置換
- index は最新 30 件のカードと月別 archive リンクのみ。全 entry JSON を埋め込まない
- related は tag inverted index で O(n × 平均タグ件数)
- static asset は content hash 名で assets/ に配置、manifest を生成
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .atom import render_atom
from .links import check_internal_links
from .models import Entry, EntriesDocument

BASE_URL = "https://branch10480.github.io/knowledge"
BASE_PATH = "/knowledge/"
INDEX_MAX = 30


@dataclass(frozen=True)
class BuildManifest:
    source_commit: str
    built_at: str
    entry_count: int
    files: Mapping[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "source_commit": self.source_commit,
            "built_at": self.built_at,
            "entry_count": self.entry_count,
            "files": dict(self.files),
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_head(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root,
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def compute_related(entries: tuple[Entry, ...]) -> Mapping[str, tuple[str, ...]]:
    """tag inverted index で関連 entry を求める（全組み合わせ比較はしない）。"""
    tag_index: dict[str, list[str]] = {}
    for e in entries:
        for t in e.tags:
            tag_index.setdefault(t, []).append(e.id)

    related: dict[str, tuple[str, ...]] = {}
    for e in entries:
        cand: dict[str, int] = {}
        for t in e.tags:
            for oid in tag_index.get(t, ()):
                if oid != e.id:
                    cand[oid] = cand.get(oid, 0) + 1
        ids = [oid for oid, _cnt in sorted(cand.items(), key=lambda kv: (-kv[1], kv[0]))]
        # published_at 降順で上位 5
        by_date = {x.id: x for x in entries}
        ids.sort(key=lambda oid: (by_date[oid].published_at, oid), reverse=True)
        related[e.id] = tuple(ids[:5])
    return related


def _make_env(templates_dir: Path, asset_names: Mapping[str, str]) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=True,
        undefined=StrictUndefined,
    )

    def url(*parts: str) -> str:
        if len(parts) == 1 and parts[0] in ("index.html", "feed.xml"):
            return BASE_PATH + parts[0]
        if len(parts) == 2 and parts[0] in ("entry", "archive"):
            return f"{BASE_PATH}{parts[0]}/{parts[1]}"
        raise ValueError(f"bad url helper args: {parts!r}")

    def asset_url(name: str) -> str:
        if name not in asset_names:
            raise ValueError(f"unknown asset: {name!r}")
        return f"{BASE_PATH}assets/{asset_names[name]}"

    env.globals["url"] = url
    env.globals["asset_url"] = asset_url
    return env


def _copy_static(static_dir: Path, out_dir: Path) -> dict[str, str]:
    """static を assets/<name> へ content hash 名でコピー。戻りは name -> asset file 名。"""
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    names: dict[str, str] = {}
    for f in sorted(static_dir.iterdir()):
        if not f.is_file():
            continue
        data = f.read_bytes()
        h = _sha256(data)[:12]
        out_name = f"{f.stem}.{h}{f.suffix}"
        (assets_dir / out_name).write_bytes(data)
        names[f.name] = out_name
    return names


def _escape_json_for_script(value: object) -> str:
    """JSON を script type=application/json 要素へ埋め込むための escape。
    `<` を \\u003c に置換して </script> 終了を防ぐ。"""
    s = json.dumps(value, ensure_ascii=False)
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def build_site(
    document: EntriesDocument,
    *,
    templates_dir: Path,
    static_dir: Path,
    output_dir: Path,
    built_at: str,
    repo_root: Path,
    base_path: str = BASE_PATH,
) -> BuildManifest:
    entries = document.entries
    related = compute_related(entries)

    # 一時ビルド先（output_dir の親に同名 .next を作り、成功後に rename）
    out_parent = output_dir.parent
    dist_next = out_parent / (output_dir.name + ".next")
    if dist_next.exists():
        shutil.rmtree(dist_next)
    dist_next.mkdir(parents=True, exist_ok=True)

    asset_names = _copy_static(static_dir, dist_next)
    env = _make_env(templates_dir, asset_names)

    # 月別アーカイブ
    months: dict[str, list[Entry]] = {}
    for e in entries:
        ym = e.published_at[:7]
        months.setdefault(ym, []).append(e)

    index_entries = entries[:INDEX_MAX]
    index_payload = [
        {
            "id": e.id,
            "title": e.title,
            "summary": e.summary[:200],
            "tags": list(e.tags),
            "published_at": e.published_at,
        }
        for e in index_entries
    ]
    data_json = _escape_json_for_script(index_payload)

    (dist_next / "index.html").write_text(
        env.get_template("index.html.j2").render(
            index_entries=index_entries, data_json=data_json,
        ),
        encoding="utf-8",
    )

    entry_dir = dist_next / "entry"
    entry_dir.mkdir(exist_ok=True)
    for e in entries:
        rel = related.get(e.id, ())
        rel_entries = tuple(x for x in entries if x.id in rel)
        host = ""
        try:
            host = e.canonical_url.split("//", 1)[1].split("/", 1)[0].replace("www.", "")
        except Exception:
            host = e.canonical_url
        (entry_dir / f"{e.id}.html").write_text(
            env.get_template("entry.html.j2").render(
                entry=e, related=rel_entries, entry_source_host=host,
            ),
            encoding="utf-8",
        )

    arch_dir = dist_next / "archive"
    arch_dir.mkdir(exist_ok=True)
    for ym, month_entries in sorted(months.items(), reverse=True):
        (arch_dir / f"{ym}.html").write_text(
            env.get_template("archive.html.j2").render(month=ym, entries=month_entries),
            encoding="utf-8",
        )

    feed = render_atom(entries, updated_at=built_at)
    (dist_next / "feed.xml").write_bytes(feed)
    (dist_next / ".nojekyll").write_text("")

    # manifest（全ファイル sha256）
    files: dict[str, str] = {}
    for p in sorted(dist_next.rglob("*")):
        if p.is_file():
            rel = p.relative_to(dist_next).as_posix()
            files[rel] = _sha256(p.read_bytes())
    manifest = BuildManifest(
        source_commit=_git_head(repo_root), built_at=built_at,
        entry_count=len(entries), files=files,
    )
    (dist_next / "manifest.json").write_text(
        json.dumps(manifest.to_json(), ensure_ascii=False, indent=2), encoding="utf-8",
    )

    # QA: 内部リンク
    report = check_internal_links(dist_next, base_path=base_path)
    if not report.ok:
        shutil.rmtree(dist_next, ignore_errors=True)
        raise RuntimeError(f"broken internal links: {report.broken}")

    # 原子的置換
    if output_dir.exists():
        shutil.rmtree(output_dir)
    os.replace(dist_next, output_dir)
    return manifest
