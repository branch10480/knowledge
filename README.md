# Knowledge

技術情報・学習ノートを蓄積する知識ベース。GitHub Pages で公開中。

🌐 https://branch10480.github.io/knowledge/

## 構成

```
entries.json    ← 全エントリの正本（JSON 配列、日付降順）
build.py        ← entries.json から index.html を生成
add_entries.py  ← 新しいエントリを entries.json に追記 → 自動ビルド
index.html      ← 生成された静的なページ（編集不要）
.lastrun        ← cron ジョブの最終実行時刻
```

## 運用

### cron ジョブ（自動収集）
毎日 9:00 JST に以下を実行：
1. Apple Developer News の Web 検索
2. AI 業界ニュース検索
3. ghq 管理リポジトリの更新チェック
4. entries.json に追記 → build.py で index.html 再生成
5. GitHub Pages に push
6. Signal + Telegram に通知

### 手動でエントリを追加する場合

```bash
cd ~/ghq/github.com/branch10480/knowledge

cat <<'EOF' | python3 add_entries.py
[
  {
    "date": "2026-08-02",
    "title": "タイトル",
    "tags": ["iOS", "Swift"],
    "content": "要約本文（マークダウン形式）",
    "source": "https://..."
  }
]
EOF

git add entries.json index.html
git commit -m "knowledge: 手動追記"
git push origin gh-pages
```

### 注意事項
- entries.json を直接編集しても OK。その後 `python3 build.py` を実行すれば index.html が再生成される
- content はマークダウン形式（見出し `#`、リスト `-`、リンク `[text](url)`、コード `` `code` `` が使える）
- URL は `https://` のみ許可
- タグは小文字推奨（`apple`, `ios`, `swift`, `ai`, `openai`, `anthropic`, `security` など）
