# HANDOFF（別セッション用の申し送り）

このファイルは Knowledge v2 パイプラインの再構築作業を別セッションで引き継ぐための申し送りです。
最終更新: 2026-08-04（本セッション終了時）

## プロジェクト概要
- リポジトリ: `/Users/branch10480/ghq/github.com/branch10480/knowledge`
- 目的: 技術情報・学習ノートの知識ベース。GitHub Pages で公開中。
  - main = ソース正本（data/, src/, config/, scripts/, schemas/, tests/, .github/）
  - gh-pages = 生成物のみ（CI が deploy）
- 分業: gpt-5.6-sol（Codex CLI `codex exec -m gpt-5.6-sol`）= 設計・レビュー、アシスタント = 実装
- cron ジョブ: `317dac27c6f8`（Knowledge v2 収集, 毎日 09:00 JST）が `./scripts/collect.sh` を実行

## 現在の状態（要約）
- **実装一式は main へコミット・push 済み**（v1 旧生成物は削除、ソース正本のみの v2 構成へ移行）。
- ブランチ: main（clean）。`.gitignore` は `.codex-*.md`、`HANDOFF.md`、`data/*.bak`、`.work.run.*/` を除外済み。
- **pytest 40件パス**（make_entry_id 修正後も確認済み）。
- data/entries.json（17件）と data/checkpoint.json はコミット済み。
- **CI（Build Knowledge Pages）は validate-build、deploy ジョブも成功**。

## 完了した実装・修正（すべて main へ push 済み）
コミット履歴（最新順）:
- `c77d1cb` fix(ci): exclude .git from check-build manifest verification
- `3547e42` fix(ci): deploy ジョブに main checkout / Python setup / deps を追加
- `35448c5` fix(ci): Install dependencies に pytest を追加（CI のテスト実行を可能に）
- `184beb5` fix: collect.sh 固定 PATH に認証ツールの場所を追加（${USER}ベース）+ .gitignore に data/*.bak
- `11ca6cc` knowledge: 収集結果とcheckpointを更新（entries 17件 + checkpoint 生成）
- `5f42294` feat: Knowledge v2 pipeline 実装一式 + v1 生成物削除

### 前セッションで完了済み（実装）
1. BLOCKER: github_api の seen 形式を checkpoint Schema 準拠に（collector で candidates から生成）
2. collect.sh: flock→mkdirベースロック（macOS対応）、固定PATH、mktemp -d、clean branch確認、commit/push を build・QA・secret scan の後に移動
3. 設定ロード: source種別ごとの必須フィールド検証（KeyError回避）
4. LLM loopback: host完全一致・scheme http・ポート制限
5. Schema: FormatChecker + 危険URL・不正日時・IP literal 検証
6. html-index enrichment: 記事本文を selected 確定後に fetch（script/style/nav 除外、quota・byte上限）
7. bootstrap: 初回は30日cutoff（checkpoint未設定時）
8. required source 失敗: collect_command が非0 を返す
9. candidate_id: collector で埋める（要約出力との照合）
10. 要約: response_format（json_schema）を外し、システムプロンプトでJSON要求 + テキスト応答からJSON抽出（ローカルLLM対応）
11. merge: 検証済み candidate を seen に追加、deferred がある場合は watermark 維持、factual_source_gate を実際に呼ぶ
12. make_entry_id: `kn_` + コロンなし hex（`_sha256(...)[7:31]`、Schema `^kn_[A-Za-z0-9]+$` 準拠）

### 本セッションで解決した問題
- make_entry_id 修正後の collect → summarize → merge → build → QA → secret scan を通しで再実行（.work で）→ build 成功（17件）、QA 全パス、secret scan OK。
- collect.sh の push 失敗（`gh: command not found`）: 固定 PATH に認証ツール（gh, git-credential-osxkeychain）の場所がないため。`/etc/profiles/per-user/${USER:-unknown}/bin` と `/opt/homebrew/bin` を PATH に追加して解決（ユーザー名はハードコードせず $USER ベース）。
- CI の pytest 失敗（`No module named pytest`）: build.yml の Install dependencies に pytest を追加。
- CI deploy 失敗（`Branch main is not allowed to deploy to github-pages`）: github-pages environment の deployment branch ポリシーが gh-pages のみ許可していたため、gh api で **main を追加**（現在 gh-pages と main の両方を許可）。
- CI deploy 失敗（`No module named knowledge`）: deploy ジョブに main の checkout がなかったため、Checkout main / Set up Python / Install dependencies を deploy ジョブに追加。

## 次のアクション（優先度順）
1. **完了**: check-build で `.git` を除外し、CI deploy の Verify deployment manifest 失敗を修正（`c77d1cb`）。
2. **完了**: push 後の CI 再実行で validate-build + deploy の成功を確認。
3. **完了**: gh-pages の deploy 成功と https://branch10480.github.io/knowledge/ の更新を確認。

## 重要な制約・注意
- **コミット・push・gh-pages 置換・通知送信は明示的承認が必要**（ユーザーに確認してから）。勝手に push しない。
- ローカル要約 LLM: `http://127.0.0.1:18080/v1`（deepseek-v4-flash）。要約は遅い（約34秒/件、8件で約5分）。cron ジョブのタイムアウト実値は要確認。
- ローカル LLM は single-session 制約。並列要約は不可。
- config/summary.yml は P1 暫定値: `max_candidates_per_run: 8`, `request_timeout_seconds: 100`, `max_retries: 1`。
- cron ジョブ（317dac27c6f8）のプロンプトは `docs/cron-prompt.md` に反映済み（実装物として）。モデルは `deepseek-v4-flash` にピン留め。
- collect.sh の固定 PATH は `${USER:-unknown}` ベース（認証ツール gh / git-credential-osxkeychain を解決するため）。
- 相談プロンプト（`.codex-consult-prompt.md`, `.codex-consult2-prompt.md`, `.codex-design-prompt.md`, `.codex-review-prompt.md`）は .gitignore で除外。不要なら削除してよい。

## 関連ファイル（主要）
- 実装: `src/knowledge/`（collector, cli, config, summarizer, builder, validate, identity, repository, feeds, github_api, models, links, atom）
- 設定: `config/sources.yml`, `config/summary.yml`
- スクリプト: `scripts/collect.sh`, `scripts/scan-secrets.sh`, `scripts/migrate_v1.py`
- Schema: `schemas/entry.schema.json`, `schemas/entries.schema.json`, `schemas/checkpoint.schema.json`, `schemas/summary-output.schema.json`
- テンプレート/静的: `templates/`, `static/`
- テスト: `tests/`（40件）
- CI: `.github/workflows/build.yml`
- 設計: `DESIGN.md`, `docs/cron-prompt.md`
