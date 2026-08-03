# Knowledge

技術情報・学習ノートを蓄積する知識ベース。GitHub Pages で公開中。

🌐 https://branch10480.github.io/knowledge/

## 構成

```
entries.json    ← 全エントリの正本（JSON 配列、日付降順）
build.py        ← entries.json から index.html / feed.xml / entry/*.html / archive/*.html を生成
add_entries.py  ← 新しいエントリを entries.json に追記 → 自動ビルド
index.html      ← メインページ（編集不要）
feed.xml        ← Atom フィード（編集不要）
entry/          ← 個別エントリページ（build.py で自動生成）
archive/        ← 月別アーカイブページ（build.py で自動生成）
.github/workflows/build.yml  ← GitHub Actions 設定
```

## 運用

### cron ジョブ（自動収集）

Hermes Agent の cron ジョブが毎日 9:00 JST に以下を実行：

1. Apple Developer News / AI ニュースを Web 検索
2. ghq 管理リポジトリの更新チェック
3. `entries.json` に追記 → `build.py` で全ページ再生成
4. GitHub Pages に push
5. Signal + Telegram に通知

### GitHub Actions（自動ビルド）

`.github/workflows/build.yml` を設定済み。以下のタイミングで動作：

| トリガー | 説明 |
|---------|------|
| `main` ブランチへの push | コード変更時に全ページを再生成 |
| `schedule: 0 9 * * *` | 毎日 9:00 JST (0:00 UTC) に定期ビルド |
| `workflow_dispatch` | GitHub UI から手動実行可能 |

**ワークフローの処理内容:**

```
main ブランチに push
  → Actions が発火
    → Ubuntu runner で Python 3.11 をセットアップ
      → build.py を実行（entries.json → index.html + feed.xml + entry/*.html + archive/*.html）
        → 差分があればコミット & gh-pages ブランチへ push
          → GitHub Pages が自動デプロイ
```

**main と gh-pages の役割:**

- `main` — ソースコード管理（build.py, template.html, entries.json, .github/）
- `gh-pages` — デプロイ用（生成された HTML ファイル一式）

Actions は main への push を監視し、ビルド結果を gh-pages に自動反映します。Hermes cron の手動 push がなくても、main への変更があれば自動的に Pages が更新されます。

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

git add entries.json index.html feed.xml entry/ archive/
git commit -m "knowledge: 手動追記"
git push origin gh-pages
```

### ビルドのみ

```bash
python3 build.py
# → index.html, feed.xml, entry/*.html, archive/YYYY-MM.html を生成
```

### 注意事項

- entries.json を直接編集しても OK。その後 `python3 build.py` を実行すれば全ページが再生成される
- content はマークダウン形式（見出し `#`、リスト `-`、リンク `[text](url)`、コード `` `code` `` が使える）
- URL は `https://` のみ許可
- タグは小文字推奨（`apple`, `ios`, `swift`, `ai`, `openai`, `anthropic`, `security` など）

---

## 開発メモ

### テーマ切り替え UI の位置

GitHub Pages の子ページ（個別エントリページ）のライト・ダークモード切り替え UI は、デザインシステムに準拠してナビゲーションバー内（`.gn-inner`）に配置します。

- **修正内容** (`build.py`):
  - `generate_single_page()` の CSS: `position:absolute;top:1rem;right:22px` を削除（デザインシステムと同様のインライン配置）
  - HTML 構造: `<button class="theme-toggle">` を `.gn-inner` の中（ブランドリンク直後）に配置

これにより、テーマ切り替えボタンが左に寄るのではなく、ナビゲーションバー内で適切な位置に表示されるようになります。
