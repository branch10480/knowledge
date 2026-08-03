# AGENTS.md

## 概要

個人向け技術知識ベース。GitHub Pages で公開（`gh-pages` ブランチ、https://branch10480.github.io/knowledge/ ）。
iOS/Apple開発、AI業界動向、技術学習メモを蓄積する。

## ブランチ運用

- デフォルトブランチは `gh-pages`。公開物はすべてこのブランチに置く（`main` は使わない）。
- push は常に `git push origin gh-pages`。

## データ形式（entries.json）

JSON 配列。各エントリは以下の形式:

```json
{
  "date": "YYYY-MM-DD",
  "title": "タイトル",
  "tags": ["Apple", "iOS"],
  "content": "要約本文（HTML または マークダウン）",
  "source": "https://..."
}
```

- `date` は `^\d{4}-\d{2}-\d{2}$` 形式。
- `source` は `http://` / `https://` のみ許可。
- `title`・`content` は必須。
- エントリは日付降順に保つ。

## エントリ追加 → ビルド

1. 新エントリを JSON 配列にまとめる。
2. `python3 add_entries.py < 新エントリJSON`（標準入力で渡す）— entries.json にマージ・日付降順ソートし、build.py を自動実行する。
3. build.py が `index.html`、`entry/*.html`、`archive/YYYY-MM.html`、`feed.xml` を再生成する。

個別ページのファイル名は `make_slug(title, date)` で生成（タイトルを英数字・日本語・`-_` 以外除去し小文字化、最大80文字、先頭に日付）。

## 収集範囲管理（cronジョブ用）

- `.lastrun` に前回収集完了時刻（UTC ISO8601、例 `2026-08-03T00:00:00Z`）を保存する。
- 収集は `.lastrun` 以降に公開された情報のみとする。
- commit & push が成功した場合にのみ `.lastrun` を更新する。
- 新着情報ゼロの場合は更新・push をスキップし、`.lastrun` も更新しない。

## 運用規約

- コミットメッセージ: `knowledge: YYYY-MM-DD の収集結果を追加`
- 公開前に機密情報チェック（トークン・認証情報・APIキーをエントリに入れない）。
- コミット対象: 変更のあった `entries.json`, `index.html`, `entry/`, `archive/`, `feed.xml`, `.lastrun`。
- 収集の優先度: Apple公式 ＞ AI業界リリース ＞ Swift/SwiftUI技術記事 ＞ その他。
