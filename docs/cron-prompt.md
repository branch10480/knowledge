# Hermes cron 用プロンプト（Knowledge v2 収集）

castleの`setup-hermes-ds4-safety.sh`が、Hermes cronジョブ
**Knowledge v2 収集**へ次の設定を冪等反映する。

- schedule: `0 9 * * *`（09:00 JST）
- provider/model: `local-main / deepseek-v4-flash`
- enabled toolsets: `knowledge-jobs`, `no_mcp`
- script: なし（agent-mode）

```text
Call knowledge_start exactly once with an empty object. Do not call terminal,
execute code, inspect files, or retry inside this turn. The tool creates a durable
job and the harness ends this turn immediately; the managed runner waits on the
shared inference queue and the scheduled one-shot capability publishes only after
all validation gates pass.
```

cronモデルは収集・Git・通知をterminalで直接実行しない。`knowledge_start`が
LLM-free collectを完了して親turnをdeferし、managed runnerが共有DS4 queueで要約する。
READY後はscheduler-proven turnに束縛したopaque capabilityで自動公開する。

Gateway再起動などでin-memory watcherを失った場合、jobはREADYのまま保持される。
次回cron turnは新規jobを作る前に、`origin_authority_kind=scheduled`のREADY jobを
同じone-shot契約で回収・公開する。要約途中のjobも再利用し、receipt前に中断したcollectは
元のdurable bindingで再収集する。旧`scripts/cron-collect.sh`、`scripts/collect.sh`、
castleの`knowledge-v2-collect.sh`はすべてexit 64でfail closedする。
