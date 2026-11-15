# claude-status-watch

Claude（Anthropic）公式ステータスページ <https://status.claude.com> を **20分おき**に見張り、
障害の **発生・悪化・復旧** のときだけ LINE（AI秘書）へ通知する GitHub Actions ジョブ。

- 実行場所: GitHub Actions（cron `*/20 * * * *`）。PCが寝ていても動く
- 依存パッケージなし（Python 標準ライブラリのみ）。AI は辞書で訳せない件名の訳だけに使う
- 変化が無い回は黙る（通知疲れ防止）。状態は `state.json` に持ち越す
- 履歴は `data/incident_log.csv` に1行ずつ残る（発生／悪化／復旧、所要時間つき）

## 通知の例（2026-11-15 から日本語だけ）

```
⚠️ Claude 障害発生（影響度: 大）
内容: 一部の利用者がApple で新規登録ができない（iOS アプリ）
対象: claude.ai（アプリ・Web 版）
状況: 調査中
公式の説明: Google かメールでの登録は使えます。（自動翻訳）
開始: 11/15 11:40 日本時間
※ 詳細 https://status.claude.com/incidents/<id>
```

```
✅ Claude 復旧
内容: 複数のモデルでエラーが増加
発生: 08/17 06:58 → 復旧確認: 08/17 07:40（日本時間）
所要: 約42分
※ 詳細 https://status.claude.com
```

- 件名・部品名は `status_ja.py` の辞書で訳す（AI 不使用）。`python status_ja.py --selftest` で過去の実件名を全部訳せるか確かめる
- 辞書で訳せない件名と公式の説明文だけ Haiku 5.5 で訳す（1日10回まで・api_budget で停止なら呼ばない・英語が残る訳は使わない）。
  Haiku が使えない回も英語は出さず「訳せませんでした」と対象・個別ページで知らせる
- 英語の原文は `state.json` と `data/incident_log.csv` にだけ残る。訳せなかった件名は Actions のログに「辞書に足す候補」と出る

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

## 新版の見張り release_watch（2026-10-21 追加）

`release_watch/release_watch.py` ＋ `.github/workflows/release_watch.yml`（毎時45分）。次の4つを前回の控え（`release_watch/state.json`）と比べ、変わったときだけ🔧システム見張りの LINE に1枚のカードで知らせる。

| 見張り先 | 何を比べるか |
|---|---|
| Claude Code 本体 | npm `@anthropic-ai/claude-code` の latest。**大きな版（2.x→3.x）のときだけ**知らせる（毎日の細かい版は黙って控えを進める） |
| モデルの顔ぶれ | モデル一覧ページの表「Claude API ID」の行。増えた＝🆕、消えた＝一覧から外れた |
| API の更新 | リリースノートの日付の節。**新モデルの発表🆕・モデルの廃止予定/廃止/利用停止⛔・利用再開✅だけ**載せ、値下げ・提供先の追加・fast mode 等は黙って控えを進める |
| Claude アプリの更新 | ヘルプセンターのリリースノートの日付の節。**モデルの利用停止⛔・再開✅だけ**（新機能は黙る。新モデルの発表は API 側と重なるので出さない） |

- 2026-10-31 絞り込み：ユーザー「細かいアプデは不要。モデルのグレードが上がったとき・使えなくなったときだけ」。サービス全体の障害（使えない）は別の障害監視 watch.yml が今までどおり知らせる
- 控えが無い見張り先は初回に控えるだけ（知らせない）。読めない回は控えを変えず、6回続いたら1回だけ知らせ、戻ったらもう1回
- 送れなかった回は控えを進めない（次の回でもう一度送る）
- 手で試す：`set DRY_RUN=1` → `python release_watch\release_watch.py`（作業用コピーで試すと本物の LINE が飛ぶので DRY_RUN は必須）
- Actions の Run workflow：`force_test=true` で「いま控えている版」を1通、`dry_run=true` で本文だけ
- 限界：デスクトップアプリの版番号そのものは見ていない（配布元の番号とストア版の番号が別系統で、知らせると混乱するため。アプリの更新はリリースノートの日付で拾う）
