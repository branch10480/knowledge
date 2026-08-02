# Knowledge プロジェクト進捗

最終更新: 2026-08-02
ブランチ: gh-pages (origin/gh-pages)
GitHub Pages: https://branch10480.github.io/knowledge/

## 完了フェーズ

### P0（基盤）✅
- [x] リポジトリ作成 (`branch10480/knowledge`、公開)
- [x] GitHub Pages 有効化 (gh-pages ブランチ)
- [x] ローカルモデル (deepseek-v4-flash) デフォルト化
- [x] Telegram ゲートウェイ設定 (通知専用チャネル)
- [x] cron ジョブ作成 (毎日 9:00 JST、配信: signal+telegram)
- [x] 初回収集・push 完了 (12 エントリ)
- [x] .lastrun 方式による差分収集
- [x] XSS 対策・URL検証・日付フォーマット検証
- [x] entries.json 正本 + build.py レンダリング方式に移行
- [x] add_entries.py (JSON 追記 → 自動ビルド)
- [x] update_html.py 削除

### P1（品質・UX）✅
- [x] README.md 作成 (構成・運用ドキュメント)
- [x] 検索ボックス (全文検索、リアルタイム)
- [x] タグフィルター (クリックで絞り込み、複数選択可)
- [x] 結果表示 (ヒット数 / 全件)
- [x] アクセシビリティ改善 (<main>、aria-label、role="button"、aria-pressed)
- [x] ライトモード対応 (prefers-color-scheme: light)
- [x] CSS 改善 (タグホバー・アクティブ状態)
- [x] template.html 分離 (テンプレートとロジックの分離)

## 未着手

### P2（拡張機能）
- [ ] 個別エントリページ（/entry/YYYY-MM-DD-slug.html）
- [ ] 月別アーカイブページ
- [ ] RSS/Atom フィード
- [ ] エントリ内の関連リンク表示

### P3（運用改善）
- [ ] GitHub Actions で main → gh-pages ビルド自動化
- [ ] Lighthouse CI スコア追跡

## 現状のファイル構成

```
knowledge/
├── entries.json      ← 正本 (12 エントリ)
├── build.py          ← ビルドスクリプト
├── template.html     ← HTML テンプレート (CSS+JS)
├── add_entries.py    ← 追記 + 自動ビルド
├── index.html        ← 生成物 (204 行)
├── README.md         ← ドキュメント
└── .lastrun          ← cron 最終実行時刻
```

## 運用中
- cron ジョブ: `Knowledge収集` (ID: `317dac27c6f8`)
  - スケジュール: `0 9 * * *` (毎日 9:00 JST)
  - 配信: signal + telegram
  - モデル: デフォルト (deepseek-v4-flash)
  - 最終動作確認: 2026-08-02 (初回収集 12 エントリ)

## 再開方法
```bash
cd ~/ghq/github.com/branch10480/knowledge
git status  # 最新状態確認
```

### 手動エントリ追加
```bash
echo '[{"date":"YYYY-MM-DD","title":"...","tags":["tag"],"content":"markdown","source":"https://..."}]' | python3 add_entries.py
git add entries.json index.html
git commit -m "knowledge: 手動追記"
git push origin gh-pages
```

### ビルドのみ
```bash
python3 build.py
```

### P2 作業開始時
- 個別ページ: template.html にエントリリンク追加 + build.py で /entry/ 生成
- RSS: build.py にフィード生成追加
- Actions: .github/workflows/ に YAML 追加
