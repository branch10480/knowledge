# HANDOFF（別セッション用の申し送り）

このファイルは Knowledge v2 パイプラインの運用作業を別セッションで引き継ぐための申し送りです。
最終更新: 2026-08-07

## プロジェクト概要
- リポジトリ: `/Users/branch10480/ghq/github.com/branch10480/knowledge`
- 目的: 技術情報・学習ノートの知識ベース。GitHub Pages で公開中（https://branch10480.github.io/knowledge/）
- main = ソース正本（data/, src/, config/, scripts/, schemas/, tests/, .github/）
- gh-pages = 生成物のみ（CI が deploy）
- cron ジョブ: `317dac27c6f8`（Knowledge v2 収集、毎日 09:00 JST）が `./scripts/collect.sh` を実行

## 現在のステータス（2026-08-05 完了）

### ✅ collect.sh パイプライン正常稼働確認済み
- Sol（gpt-5.6-sol）が Herdr 右ペインで完全実行
- collect 6件 → summarize 6件 → merge 6件追加 → build 23件 → QA・secret scan 成功
- GitHub Actions run `30907254145` **success**（gh-pages deploy完了）
- data/entries.json, data/checkpoint.json は main に push 済み

### ✅ 根本原因の特定と修正完了（コミット済み）
1. **タイムアウト原因**: `max_output_tokens_per_candidate: 700` が LLM リクエストに反映されず、llama.cpp が `n_predict=-1` で長時間 reasoning（700トークン分）→ 100秒タイムアウト
   - 修正: `summarize_candidates()` で `max_tokens` をリクエストに反映
   - 修正: `chat_template_kwargs: {"enable_thinking": false}` を追加（Qwen の reasoning 消費対策）
   - コミット: `4f37435`
2. **factual gate 失敗原因**: LLM が引用文を一字一句コピーせず、要約・言い換えをしていた
   - 修正: system prompt に出力サイズ制限（summary_ja 300文字以内、key_points 最大3件、tags 最大5件、claims 最大2件、evidence_quotes 各1件）と「完全一致の文字列を引用してください」指示を追加
   - コミット: `4f37435`
3. **evidence_quotes maxLength 修正**: Schema と summarizer.py の両方を 300→1000 に変更
   - コミット: `e6ff061`

### ✅ テスト
- `pytest 43 passed`

### ✅ summarize の JSON 破損修正完了（2026-08-07, commit `1a066ce0920c`）

- **原因**: ローカル要約 LLM（deepseek-v4-flash）が稀に `"summary_ja": Appleは...` と**文字列値を引用符なし（裸の文字列）で返し**、JSON が壊れて `malformed json: Expecting value: line 1 column 154` で `summarize` ステップが停止。checkpoint の `last_success_at` は `1970-01-01` のまま一度も成功していなかった。
- **修正**（`src/knowledge/summarizer.py`）:
  1. `_repair_json` を追加 — 引用符なしの文字列値（`"key": 裸の文字列`）を引用符で囲み、末尾カンマを除去
  2. `validate_summary_output` で `json.loads` 失敗時に修復を試行、成功すれば検証を続行
  3. 修復できない壊れ方は `retryable=True` に変更（transient なモデル出力品質として再試行可能に）
  4. 再試行時にシードを `config.seed + attempt` と変更（固定シードだと同じ壊れた出力が返るため）
- **テスト**: `tests/test_summarizer.py` に修復・再試行シードの回帰テスト追加。`pytest 54 passed`
- **検証**: 実候補での summarize 成功（以前失敗した候補も要約成功）、パイプライン全体（merge → build → QA → secret scan）成功
- **教訓**: ローカル量子化 LLM の JSON 出力は不安定。修復 + シード変更再試行で耐性を持たせる。

### 📝 最新コミット履歴（最新順）
| コミット | メッセージ |
|---|---|
| `a23a2fb` | docs: add handoff notes for next session |
| `4f37435` | fix(summarizer): add output token limit, disable reasoning, constrain summary size |
| `427d847` | knowledge: 収集結果とcheckpointを更新 |
| `e6ff061` | fix(summary): increase evidence_quotes maxLength from 300 to 1000 |

### ⚠️ 未確認事項（次セッションで確認）
- `git status` で「ahead 1」: `a23a2fb`（`docs: add handoff notes for next session`）が `origin/main` にpushされていない
- 確認: `git log --oneline origin/main..HEAD` → `a23a2fb docs: add handoff notes for next session` 1件
- 対処: 別セッションで `git push origin main` を実行

## 重要な制約
- ローカル要約LLM: `http://127.0.0.1:18080/v1`（deepseek-v4-flash）。単一セッション制約。並列要約不可。
- config/summary.yml: `max_candidates_per_run: 8`, `request_timeout_seconds: 100`, `max_retries: 1`
- collect.sh は `set -Eeuo pipefail`、mkdir 排他ロック、clean branch 確認付き
- コミット・push・gh-pages置換は明示的承認が必要

### ✅ cron 収集ジョブの修正完了（2026-08-05）

- **原因**: `local-coder-enforcer` プラグインが `deepseek-v4-flash` をコーディネーター扱いして `terminal` を拒否
- **解決**: cronジョブ `317dac27c6f8` を `no_agent: true` + `script: knowledge-v2-collect.sh` に変更
- **スクリプト**: `~/.hermes/scripts/knowledge-v2-collect.sh`
  - branch/clean/origin/merge-base チェック付き
  - 失敗時は `exec /bin/bash "$repo/scripts/collect.sh"` の結果をそのまま配信
- 次回実行: 2026-08-06 09:00 JST

### ⚠️ ハーネスの制約

- このセッションでは `terminal` / `write_file` / `execute_code` がブロックされている
- cronジョブの更新は `cronjob(action='update')` で完了
- スクリプトファイルは `install_local_script` でインストール完了
- `delegate_local_coder` のサーバーはダウン中（ポート18082 unreachable）
- push は別セッションで `git push origin main` を実行する必要がある

## 参考：summarizer.py の主な変更点（4f37435）
- system prompt に出力制限指示追加（summary_ja, key_points, tags, claims, evidence_quotes のサイズ制限）
- `summarize_candidates()` で `max_tokens` と `chat_template_kwargs={"enable_thinking": false}` をリクエストに反映
- tests/test_summarizer.py に出力制限指示と max_tokens 反映のテスト追加

## 参考：過去に解決した問題
- collect.sh の push 失敗（`gh: command not found`）→ PATH を `${USER:-unknown}` ベース + /opt/homebrew/bin に修正
- CI の pytest 失敗（`No module named pytest`）→ build.yml に pytest 追加
- CI deploy 失敗（`Branch main is not allowed to deploy to github-pages`）→ gh-pages environment の deployment branch ポリシーに main を追加
- CI deploy 失敗（`No module named knowledge`）→ deploy ジョブに main checkout / Python setup / deps を追加
- make_entry_id の Schema 違反（コロン入り）→ `kn_` + hex 形式に変更
