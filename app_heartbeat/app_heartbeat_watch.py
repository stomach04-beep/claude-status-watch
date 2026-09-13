# -*- coding: utf-8 -*-
"""
app_heartbeat_watch.py — 買い時アラートアプリ（EtfBuyAlert）の生存監視

何をするか:
    Notion「投資ウォッチリスト」の生存監視用の行（ティッカー _APP_HEARTBEAT）の
    「アプリ最終同期」を見て、一定時間更新が無ければ LINE で知らせる。

なぜ必要か:
    アプリが Doze・電池最適化・force-stop・トークン失効で黙って止まっても、
    「通知が来ない」という形でしか現れず、押し目が来ていないのと区別できない。
    止まっているアプリに通知させることはできないので、外から見張る。

なぜこのリポジトリ（公開）で動かすか（2026-09-13 移設）:
    以前は自宅PCのタスク StockLadderWatch が毎時動かしていたが、PCが寝ている時間が長く、
    平日の東京場中に動けていたのは約36%だった。GitHub Actions なら PC と無関係に毎時動く。
    非公開リポの無料枠が残り少ないため、価格・建玉を一切含まないこの監視だけを公開リポに置く
    （ラダー監視そのものは非公開リポ claud-code の ladder_watch.yml が3時間おきに担当）。
    公開ログに出すのは「最終同期の時刻と経過時間」「通知件数」だけ。
    旧版にあった週次サマリー（★銘柄の一覧を含む）は 2026-07-30 から止めていたので移設時に外した。

状態:
    state.json（通知済みか・前回の通知時刻だけ）。変化したときだけワークフローがコミットする。
    毎時変わる値（実行時刻・最終同期時刻）は書かない＝毎時コミットが積もらない。

使い方:
    python app_heartbeat/app_heartbeat_watch.py                # 通常監視（毎時）
    python app_heartbeat/app_heartbeat_watch.py --dry-run      # LINE送信せず画面に出す
    python app_heartbeat/app_heartbeat_watch.py --test-stale 9 # 9時間停止しているように見せる（state は更新しない）
    必要な環境変数: NOTION_TOKEN, LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID
    依存パッケージ無し（標準ライブラリのみ）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
JST = timezone(timedelta(hours=9))

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_TIMEOUT_SEC = 30

NOTION_DB_ID = "8b243e59af5f453b87db5454dc1528ee"   # 投資ウォッチリスト
NOTION_VERSION = "2022-06-28"                        # アプリ側 NotionClient と同じ版に合わせる
NOTION_TIMEOUT_SEC = 30

# アプリ側と同じ名前。ここがズレると黙って「見つからない」になる
HEARTBEAT_TICKER = "_APP_HEARTBEAT"
PROP_HEARTBEAT = "アプリ最終同期"

# 何時間途切れたら異常とみなすか。
# 2026-08-16に5時間へ引き下げた。旧値7時間は「AlarmHealthWorkerの6時間＋余裕1時間」で
# 決めていたが、実測で5〜7時間の空白（東京市場の立会時間が丸ごと抜ける規模）が何日も続いていたのに、
# しきい値の下をすり抜けて一度も発報しなかった。
# 買い時アラート v1.21 から価格チェックはアラーム自己連鎖で毎時0分に走るので、
# 5時間の沈黙＝5回連続の取りこぼしであり、明確に異常と言える。
# 下げすぎると（電波の無い場所に数時間いた等で）誤報が増え、無視の習慣がつくので5時間で止める。
STALE_HOURS = 5
# 停止が続いている間、何時間おきに再通知するか（毎時鳴らすとうるさく、無視の習慣がつく）
REALERT_HOURS = 24

TEST_BANNER = "🧪【動作テスト】本物の通知ではありません\n\n"

# state.json に残す項目（これ以外は書かない）
STATE_KEYS = ("stale_fired", "last_alert_at", "missing_fired")


def _log(msg: str) -> None:
    """Actions のログへ出す。公開ログなので、価格・銘柄・トークンなどは渡さないこと"""
    print(f"{datetime.now(JST):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _clean_secret(v: str) -> str:
    """BOM・引用符・前後空白を除去（latin-1エンコード事故の予防）"""
    return v.strip().strip('"').strip("'").lstrip("﻿")


def load_env() -> dict:
    """Secrets は環境変数で渡される"""
    keys = ("NOTION_TOKEN", "LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID")
    return {k: _clean_secret(os.environ.get(k, "")) for k in keys}


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"stale_fired": False, "last_alert_at": None, "missing_fired": False}
    with open(STATE_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(st: dict) -> None:
    """通知の状態だけ書く（毎回変わる値は書かない＝変化が無ければファイルも変わらない）"""
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({k: st.get(k) for k in STATE_KEYS}, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _post_json(url: str, headers: dict, body: dict, timeout: int) -> tuple[int, str]:
    """JSON を POST して (HTTPステータス, 本文) を返す。4xx/5xx も例外にせず値で返す"""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def send_line(env: dict, text: str, dry_run: bool = False,
              test_mode: bool = False) -> bool:
    # テスト送信は本物と見分けが付くようにする（紛らわしい通知は事故のもと）
    if test_mode:
        text = TEST_BANNER + text
    if dry_run:
        print("\n----- LINE送信内容（dry-run） -----")
        print(text)
        print("----------------------------------\n")
        return True
    token = env.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = env.get("LINE_USER_ID", "")
    if not token or not user_id:
        _log("LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID が無いため送信スキップ")
        return False
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"to": user_id, "messages": [{"type": "text", "text": text[:4800]}]}
    try:
        code, _ = _post_json(LINE_PUSH_URL, headers, body, LINE_TIMEOUT_SEC)
    except Exception as e:                                       # noqa: BLE001
        _log(f"LINE送信失敗: {type(e).__name__}")
        return False
    if code != 200:
        # 応答本文は出さない（公開ログ）。ステータスだけで切り分けられる
        _log(f"LINE送信失敗: HTTP {code}")
        return False
    _log("LINE送信: 成功")
    return True


# ───────────────────────────────────────────────
# Notion から生存の証を取る
# ───────────────────────────────────────────────

def notion_query(env: dict, body: dict) -> dict:
    """投資ウォッチリストDBを検索する。4xxはリトライしても直らないので即あきらめる。"""
    token = env.get("NOTION_TOKEN", "")
    if not token:
        raise RuntimeError("NOTION_TOKEN が環境変数に見つかりません")
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    last = None
    for attempt in range(3):
        try:
            code, text = _post_json(url, headers, body, NOTION_TIMEOUT_SEC)
        except Exception as e:                                   # noqa: BLE001
            last = type(e).__name__
        else:
            if code == 200:
                return json.loads(text)
            if code in (400, 401, 403, 404):
                raise RuntimeError(f"Notion HTTP {code}")
            last = f"HTTP {code}"
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Notion問い合わせに3回失敗: {last}")


def _prop_date(props: dict, name: str) -> str | None:
    d = (props.get(name) or {}).get("date")
    return (d or {}).get("start")


def fetch_heartbeat(env: dict) -> datetime | None:
    """生存監視行の「アプリ最終同期」を取る。行が無ければ None（＝設定漏れ）。"""
    body = {
        "filter": {"property": "ティッカー", "rich_text": {"equals": HEARTBEAT_TICKER}},
        "page_size": 5,
    }
    rows = notion_query(env, body).get("results", [])
    if not rows:
        return None
    raw = _prop_date(rows[0].get("properties", {}), PROP_HEARTBEAT)
    if not raw:
        return None
    # Notionは "2026-07-28T12:34:56.000+09:00" 形式
    return _parse_jst(raw.replace("Z", "+00:00"))


def _parse_jst(raw: str) -> datetime:
    """ISO形式の日時を読む。タイムゾーンが無ければ日本時間とみなす（旧PC版の state は時差なしで書いていた）"""
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=JST)


# ───────────────────────────────────────────────
# 通知文面
# ───────────────────────────────────────────────

def _fmt_jst(dt: datetime) -> str:
    return dt.astimezone(JST).strftime("%m/%d %H:%M")


def msg_stale(last: datetime, hours: float) -> str:
    return (
        "【⚠️買い時アラート停止疑い】\n\n"
        f"最後の同期から {hours:.1f}時間 経過しています\n"
        f"（最終同期 {_fmt_jst(last)} ／ 正常なら毎時0分に1回）\n\n"
        "▼確認すること\n"
        "1. スマホでアプリを開き、右上の更新ボタンを押す\n"
        "2. 設定タブの「電池最適化の除外」が✅除外済みか確認\n"
        "3. 直らなければNotionトークンの失効を疑う\n\n"
        "※この間、押し目が来ても通知は出ていません"
    )


def msg_recovered(last: datetime) -> str:
    return (
        "【買い時アラート復帰】\n\n"
        f"同期を確認しました（{_fmt_jst(last)}）\n"
        "監視は通常どおり続いています"
    )


def msg_missing() -> str:
    return (
        "【⚠️生存監視の設定未完了】\n\n"
        "Notionの生存監視行（_APP_HEARTBEAT）に「アプリ最終同期」がまだ記録されていません\n\n"
        "▼確認すること\n"
        "1. アプリを最新版に更新したか\n"
        "2. アプリを開いて更新ボタンを押したか\n"
        "3. Notionに _APP_HEARTBEAT の行があり、アプリ監視=ON か\n\n"
        "※記録されるまで、アプリの停止を検知できません"
    )


# ───────────────────────────────────────────────
# 本体
# ───────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="LINE送信せず画面に出す（state も更新しない）")
    ap.add_argument("--test-stale", type=float,
                    help="指定時間だけ停止しているように見せて挙動を試す（stateは更新しない）")
    args = ap.parse_args()

    env = load_env()
    st = load_state()
    test_mode = args.test_stale is not None
    sent = 0
    rc = 0

    try:
        hb = fetch_heartbeat(env)
    except Exception as e:                                       # noqa: BLE001
        msg = str(e)
        if msg.startswith("Notion HTTP 4") or "NOTION_TOKEN" in msg:
            # トークン失効・権限・DB ID の誤りは待っても直らない。緑のまま黙ると
            # 「見張りが見張れていない」状態が続くので、run を赤にして気付けるようにする
            _log(f"Notion の設定エラーで判定できない: {msg}")
            return 1
        # 一時的な通信失敗はアプリの異常ではない。誤報を出さず、次の毎時実行に任せる
        _log(f"Notion取得失敗のため判定スキップ（一時的）: {msg}")
        return 0

    if test_mode:
        hb = datetime.now(JST) - timedelta(hours=args.test_stale)
        _log(f"テスト: 最終同期を {args.test_stale} 時間前として判定する")

    if hb is None:
        # 生存監視行が無い／まだ書かれていない
        _log("アプリ最終同期が未記録")
        if not st.get("missing_fired"):
            if send_line(env, msg_missing(), args.dry_run, test_mode):
                sent += 1
                st["missing_fired"] = True
            else:
                rc = 1
    else:
        st["missing_fired"] = False
        age_h = (datetime.now(JST) - hb).total_seconds() / 3600
        _log(f"アプリ最終同期 {_fmt_jst(hb)}（{age_h:.1f}時間前）")

        if age_h >= STALE_HOURS:
            # 停止中は毎時鳴らさない（無視の習慣がつく）。初回と、その後は24時間おき
            last_alert = st.get("last_alert_at")
            due = True
            if st.get("stale_fired") and last_alert:
                elapsed = (datetime.now(JST) - _parse_jst(last_alert)).total_seconds() / 3600
                due = elapsed >= REALERT_HOURS
            if due:
                if send_line(env, msg_stale(hb, age_h), args.dry_run, test_mode):
                    sent += 1
                    st["stale_fired"] = True
                    st["last_alert_at"] = datetime.now(JST).isoformat(timespec="seconds")
                    _log("停止疑いを通知")
                else:
                    rc = 1      # 異常を見つけたのに知らせられていない＝赤にする
            else:
                _log("停止継続中だが再通知の時刻に達していないため送らない")
        elif st.get("stale_fired"):
            if send_line(env, msg_recovered(hb), args.dry_run, test_mode):
                sent += 1
                st["stale_fired"] = False
                st["last_alert_at"] = None
                _log("復帰を通知")
            else:
                rc = 1

    if test_mode or args.dry_run:
        _log("テスト実行／dry-run のため state.json は更新しない")
    else:
        save_state(st)
    _log(f"完了: 通知 {sent}件")
    return rc


if __name__ == "__main__":
    # 例外で落ちるときも「なぜ落ちたか」を必ずログへ残す（原因が消えないように）
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as _e:                                      # noqa: BLE001
        _log(f"アプリ生存監視 異常終了: {type(_e).__name__}: {_e}")
        raise SystemExit(1)
