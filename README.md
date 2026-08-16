# claude-status-watch

Claude（Anthropic）公式ステータスページ <https://status.claude.com> を **20分おき**に見張り、
障害の **発生・悪化・復旧** のときだけ LINE（AI秘書）へ通知する GitHub Actions ジョブ。

- 実行場所: GitHub Actions（cron `*/20 * * * *`）。PCが寝ていても動く
- AI呼び出しなし・ログイン不要・依存パッケージなし（Python 標準ライブラリのみ）
- 変化が無い回は黙る（通知疲れ防止）。状態は `state.json` に持ち越す
- 履歴は `data/incident_log.csv` に1行ずつ残る（発生／悪化／復旧、所要時間つき）

## 通知の例

```
⚠️ Claude 障害発生（影響度: 重大）
件名: Service disruption on Claude services
対象: claude.ai、Claude API (api.anthropic.com)、Claude Code
開始: 08/17 06:58 JST
最新: We are investigating elevated error rates...
https://status.claude.com
```

```
✅ Claude 復旧
件名: Service disruption on Claude services
発生: 08/17 06:58 JST → 復旧確認: 08/17 07:40 JST
所要: 約42分
https://status.claude.com
```

## 手動実行（Actions タブ → Run workflow）

| 入力 | 意味 |
|---|---|
| `force_test=true` | 状態に関係なくテスト文面を1通 LINE に送る（配線確認） |
| `dry_run=true` | 送信せず本文をログに出すだけ |

## 手元で動かす

```
set DRY_RUN=1
python claude_status_watch.py
```

## 限界（正直に）

- 公式ページに載らない不調（自分のアカウントだけ・特定機能だけ）は拾えない
- 公式ページ自体が障害を載せるまで数分〜十数分の遅れがある。さらに20分間隔＋Actionsの遅延が乗る
- 復旧時刻は「復旧を観測した回の時刻」なので、実際の復旧より最大20分ほど遅く出る

## 必要な Secrets

`LINE_CHANNEL_ACCESS_TOKEN` / `LINE_USER_ID`（他ジョブと同じもの）
