# Knowledge プロジェクト進捗

最終更新: 2026-08-02
ブランチ: gh-pages (origin/gh-pages)
GitHub Pages: https://branch10480.github.io/knowledge/

## 完了フェーズ

### P0（基盤）✅
- [x] リポジトリ作成 (`branch10480/knowledge`、公開)
- [x] gh-pagesブランチ初期化 + GitHub Pages有効化
- [x] `entries.json` 正本 + `build.py` ビルド方式
- [x] `add_entries.py` エントリ追記スクリプト

### P1（検索・フィルター・アクセシビリティ）✅
- [x] リアルタイム検索
- [x] タグフィルター
- [x] ARIA属性対応
- [x] ライトモード対応
- [x] template.html 分離
- [x] README.md

### P2（個別ページ・アーカイブ・RSS）✅
- [x] Atom フィード (`feed.xml`)
- [x] 個別エントリページ (`entry/<slug>.html`)
- [x] 月別アーカイブ (`archive/YYYY-MM.html`)
- [x] 関連エントリ表示
- [x] アーカイブナビ

### P3（CI/CD）✅
- [x] GitHub Actions ワークフロー (`.github/workflows/build.yml`)
- [x] README に Actions 説明追加

### デザイン刷新 ✅
- [x] Toshi Design System v0.7.0 準拠に全ページ改修
  - `globalnav`（sticky ヘッダー）
  - `hero`（見出しエリア）
  - `entry-card`（カード型エントリ表示）
  - `tag-pill`（ピル型タグ）
  - CSS トークン完全準拠 (--bg, --tint, --sh-1 など)
  - UDEV Gothic 35LG フォント
  - color-scheme / prefers-color-scheme / localStorage テーマ切替

## 次フェーズ

P4: GitHub Actions の main→gh-pages ビルド連携完了確認 + cronジョブのbuild.py対応
