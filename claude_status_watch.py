# -*- coding: utf-8 -*-
"""
claude_status_watch.py ― Claude（Anthropic）公式ステータスページを見張り、
障害の「発生」と「復旧」だけを LINE に知らせる。

【何をするスクリプトか】（VBAでいうと「Webから状態を取ってきて、前回と違ったらMsgBox」）
  1. https://status.claude.com/api/v2/summary.json を読む（ログイン不要・無料・AI不使用）
  2. 「未解決の障害があるか／どの部品が落ちているか」を判定する
  3. 前回の状態（state.json）と比べ、変化があったときだけ LINE に push する
       - 障害が新しく出た      → ⚠️ 発生通知
       - 影響度が上がった      → ⚠️ 悪化通知
       - 全部解決した          → ✅ 復旧通知（所要時間つき）
     変化が無ければ黙る（20分おきに走るので、毎回鳴らすと通知疲れになる）
  4. 障害の履歴を data/incident_log.csv に1行ずつ残す（あとで「今月何回落ちた」を数えられる）

【実行場所】GitHub Actions（cron 20分おき）。PCが寝ていても動く。
【環境変数】
  LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID … 通知先（無ければ標準出力に本文を出すだけ）
  DRY_RUN=1                                … 送信せず本文を標準出力（動作確認用）
  FORCE_TEST=1                             … 状態に関係なくテスト文面を1通送る（配線確認用）

【安全境界】読むのは公開ステータスページだけ。書くのは state.json と CSV だけ。
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Windows ローカルで動かしても日本語 print が化けないように
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ─────────────────────────────────────────────
# 設定（しきい値・URLはここだけに書く＝単一の真実の源）
# ─────────────────────────────────────────────
STATUS_URL = "https://status.claude.com/api/v2/summary.json"   # 旧 status.anthropic.com はここへ転送される
STATUS_PAGE_URL = "https://status.claude.com"
LINE_API_URL = "https://api.line.me/v2/bot/message/push"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_CSV = os.path.join(BASE_DIR, "data", "incident_log.csv")

FETCH_RETRY = 3            # ステータスページ取得のリトライ回数
FETCH_TIMEOUT_SEC = 20     # 1回あたりのタイムアウト
FETCH_FAIL_NOTIFY_AT = 3   # 連続この回数取れなかったら「ステータスページ自体に届かない」を1回だけ知らせる

# 影響度の順位（悪化判定に使う）。none は「お知らせ」レベルなので障害扱いしない
IMPACT_RANK = {"none": 0, "minor": 1, "major": 2, "critical": 3}
IMPACT_JA = {"minor": "軽微", "major": "大", "critical": "重大", "none": "情報"}

# 解決済みとみなす incident の status
RESOLVED_STATUSES = {"resolved", "postmortem"}

JST = timezone(timedelta(hours=9))


# ─────────────────────────────────────────────
# 小道具
# ─────────────────────────────────────────────
def log(msg: str) -> None:
    """時刻つきで標準出力に出す（Actions のログで追えるように）"""
    print(f"[{datetime.now(JST).strftime('%H:%M:%S')}] {msg}", flush=True)


def _clean_secret(v: str | None) -> str:
    """Secrets の BOM・前後空白を除去（過去に BOM 混入で全送信スキップした教訓）"""
    return (v or "").lstrip("﻿").strip()


def parse_iso(s: str | None) -> datetime | None:
    """'2026-08-16T21:58:56.949Z' 形式を datetime(UTC) にする"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_jst(dt: datetime | None) -> str:
    """JST の 'M/D HH:MM' 表記"""
    if not dt:
        return "?"
    return dt.astimezone(JST).strftime("%m/%d %H:%M")


def fmt_duration(delta: timedelta | None) -> str:
    """所要時間を「1時間23分」形式に"""
    if delta is None:
        return "?"
    m = int(delta.total_seconds() // 60)
    h, m = divmod(m, 60)
    return f"{h}時間{m}分" if h else f"{m}分"


# ─────────────────────────────────────────────
# 1) ステータスページを読む
# ─────────────────────────────────────────────
def fetch_summary() -> dict | None:
    """summary.json を取る。失敗したらリトライし、それでも駄目なら None"""
    req = urllib.request.Request(STATUS_URL, headers={"User-Agent": "claude-status-watch/1.0"})
    for i in range(1, FETCH_RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as r:
                data = json.loads(r.read().decode("utf-8"))
            if "components" in data and "incidents" in data:
                return data
            log(f"取得したJSONの形が想定外（{i}回目）")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            log(f"取得失敗（{i}回目）: {e}")
        time.sleep(5 * i)
    return None


# ─────────────────────────────────────────────
# 2) 「いま障害中か」を判定する
# ─────────────────────────────────────────────
def summarize(data: dict) -> dict:
    """summary.json から判定に必要な部分だけ抜き出す。

    戻り値:
      incidents : {incident_id: {name, impact, status, started, components[], last_update}}
                  未解決かつ impact が none 以外のものだけ
      degraded  : 正常でない部品名のリスト（例: ["Claude Code", "claude.ai"]）
      is_down   : 上のどちらかが空でなければ True
    """
    incidents = {}
    for inc in data.get("incidents", []):
        status = inc.get("status", "")
        impact = inc.get("impact", "none")
        if status in RESOLVED_STATUSES or IMPACT_RANK.get(impact, 0) == 0:
            continue
        updates = inc.get("incident_updates") or []
        latest = updates[0].get("body", "") if updates else ""
        incidents[inc["id"]] = {
            "name": inc.get("name", ""),
            "impact": impact,
            "status": status,
            "started": inc.get("started_at") or inc.get("created_at"),
            "components": [c.get("name", "") for c in inc.get("components", [])],
            "last_update": latest[:200],
        }

    degraded = [
        c.get("name", "")
        for c in data.get("components", [])
        if not c.get("group") and c.get("status") != "operational"
    ]
    return {"incidents": incidents, "degraded": degraded, "is_down": bool(incidents or degraded)}


# ─────────────────────────────────────────────
# 3) 前回状態との比較 → 通知文を組み立てる
# ─────────────────────────────────────────────
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    """壊れた state を残さないよう、一時ファイルに書いてから置き換える"""
    tmp = STATE_FILE + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def build_messages(prev: dict, cur: dict, now: datetime) -> tuple[list[str], dict]:
    """前回(prev)と今回(cur)を比べ、送るべき LINE 本文のリストと新しい state を返す。

    state の形:
      {"is_down": bool, "since": ISO文字列 or None, "incidents": {id: {impact,...}},
       "degraded": [...], "fetch_fail": int, "checked_at": ISO}
    """
    msgs: list[str] = []
    prev_incs: dict = prev.get("incidents", {}) or {}
    cur_incs: dict = cur["incidents"]

    # ── 新規に出た障害 ／ 影響度が上がった障害 ──
    for iid, inc in cur_incs.items():
        old = prev_incs.get(iid)
        if old is None:
            head = "⚠️ Claude 障害発生"
        elif IMPACT_RANK.get(inc["impact"], 0) > IMPACT_RANK.get(old.get("impact"), 0):
            head = "⚠️ Claude 障害が悪化"
        else:
            continue  # 変化なし（status が investigating→monitoring になった程度では鳴らさない）
        comps = "、".join(inc["components"]) or "（対象未記載）"
        body = (
            f"{head}（影響度: {IMPACT_JA.get(inc['impact'], inc['impact'])}）\n"
            f"件名: {inc['name']}\n"
            f"対象: {comps}\n"
            f"開始: {fmt_jst(parse_iso(inc['started']))} JST\n"
        )
        if inc["last_update"]:
            body += f"最新: {inc['last_update']}\n"
        body += STATUS_PAGE_URL
        msgs.append(body)

    # ── incident は無いが部品だけ黄色（degraded）になった場合 ──
    prev_deg = set(prev.get("degraded", []) or [])
    cur_deg = set(cur["degraded"])
    if not cur_incs and cur_deg and cur_deg - prev_deg:
        msgs.append(
            "⚠️ Claude 一部機能が不調（正式な障害告知はまだ）\n"
            f"対象: {'、'.join(sorted(cur_deg))}\n"
            f"検知: {fmt_jst(now)} JST\n{STATUS_PAGE_URL}"
        )

    # ── 復旧（前回は障害中 → 今回は全部正常）──
    if prev.get("is_down") and not cur["is_down"]:
        since = parse_iso(prev.get("since"))
        # 復旧時刻はページ側の resolved_at のほうが正確だが、
        # summary.json には解決済み incident が残らないので「今回の観測時刻」で代用する
        names = "／".join(v.get("name", "") for v in prev_incs.values()) or "（部品の不調）"
        msgs.append(
            "✅ Claude 復旧\n"
            f"件名: {names}\n"
            f"発生: {fmt_jst(since)} JST → 復旧確認: {fmt_jst(now)} JST\n"
            f"所要: 約{fmt_duration(now - since if since else None)}\n{STATUS_PAGE_URL}"
        )

    # ── 新しい state ──
    if cur["is_down"]:
        # 継続中なら「since」は最初に検知した時刻を引き継ぐ。新規なら最古の incident 開始時刻
        if prev.get("is_down") and prev.get("since"):
            since_iso = prev["since"]
        else:
            starts = [parse_iso(v["started"]) for v in cur_incs.values() if v.get("started")]
            since_iso = (min(starts) if starts else now).isoformat()
    else:
        since_iso = None

    new_state = {
        "is_down": cur["is_down"],
        "since": since_iso,
        "incidents": cur_incs,
        "degraded": sorted(cur_deg),
        "fetch_fail": 0,
        "checked_at": now.isoformat(),
    }
    return msgs, new_state


# ─────────────────────────────────────────────
# 4) 履歴 CSV に残す
# ─────────────────────────────────────────────
def append_log(rows: list[list[str]]) -> None:
    """data/incident_log.csv に追記（ヘッダは初回だけ）"""
    if not rows:
        return
    os.makedirs(os.path.dirname(LOG_CSV), exist_ok=True)
    new_file = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["検知時刻JST", "種別", "incident_id", "件名", "影響度", "対象", "開始JST", "所要"])
        w.writerows(rows)


def log_rows(prev: dict, new_state: dict, now: datetime) -> list[list[str]]:
    """通知と同じ条件で CSV 行を作る（発生・悪化・復旧）"""
    rows = []
    prev_incs = prev.get("incidents", {}) or {}
    ts = now.astimezone(JST).strftime("%Y-%m-%d %H:%M")
    for iid, inc in new_state["incidents"].items():
        old = prev_incs.get(iid)
        if old is None:
            kind = "発生"
        elif IMPACT_RANK.get(inc["impact"], 0) > IMPACT_RANK.get(old.get("impact"), 0):
            kind = "悪化"
        else:
            continue
        rows.append([ts, kind, iid, inc["name"], inc["impact"], "|".join(inc["components"]),
                     fmt_jst(parse_iso(inc["started"])), ""])
    if prev.get("is_down") and not new_state["is_down"]:
        since = parse_iso(prev.get("since"))
        rows.append([ts, "復旧", "|".join(prev_incs.keys()),
                     "／".join(v.get("name", "") for v in prev_incs.values()),
                     "", "", fmt_jst(since), fmt_duration(now - since if since else None)])
    return rows


# ─────────────────────────────────────────────
# 5) LINE 送信（戻り値を必ず見る＝虚偽ログ防止）
# ─────────────────────────────────────────────
def send_line(text: str) -> bool:
    """LINE Messaging API に push。成功なら True。DRY_RUN や未設定なら本文を出して False"""
    token = _clean_secret(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
    user_id = _clean_secret(os.environ.get("LINE_USER_ID"))
    if os.environ.get("DRY_RUN") == "1" or not token or not user_id:
        why = "DRY_RUN" if os.environ.get("DRY_RUN") == "1" else "LINE secrets 未設定"
        log(f"（{why}のため送信せず）本文↓\n{text}")
        return False
    payload = json.dumps({"to": user_id, "messages": [{"type": "text", "text": text[:4800]}]}).encode("utf-8")
    req = urllib.request.Request(
        LINE_API_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = 200 <= r.status < 300
            log(f"LINE送信: HTTP {r.status}")
            return ok
    except urllib.error.HTTPError as e:
        log(f"LINE送信失敗: HTTP {e.code} {e.read()[:200]!r}")
    except Exception as e:
        log(f"LINE送信失敗: {e}")
    return False


# ─────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────
def main() -> int:
    now = datetime.now(timezone.utc)
    prev = load_state()

    # 配線テスト（workflow_dispatch から手で叩く用）
    if os.environ.get("FORCE_TEST") == "1":
        ok = send_line(f"🔧 claude-status-watch 配線テスト\n{fmt_jst(now)} JST に送信。障害監視は20分おきに稼働中。\n{STATUS_PAGE_URL}")
        return 0 if ok or os.environ.get("DRY_RUN") == "1" else 1

    data = fetch_summary()
    if data is None:
        # ステータスページ自体に届かない。state は前回のまま維持し、連続失敗回数だけ増やす
        fails = int(prev.get("fetch_fail", 0)) + 1
        prev["fetch_fail"] = fails
        prev["checked_at"] = now.isoformat()
        save_state(prev)
        log(f"ステータスページ取得不可（連続{fails}回）")
        if fails == FETCH_FAIL_NOTIFY_AT:
            send_line(f"⚠️ status.claude.com に{fails}回連続で届きません（{fmt_jst(now)} JST）。\n"
                      f"Claude本体の障害かは不明。手で確認→ {STATUS_PAGE_URL}")
        return 2  # 取れなかった回は失敗として残す（緑にしない）

    cur = summarize(data)
    log(f"全体status={data.get('status', {}).get('description')} / 未解決障害={len(cur['incidents'])}件 / 不調部品={cur['degraded'] or 'なし'}")

    msgs, new_state = build_messages(prev, cur, now)
    if prev.get("fetch_fail", 0) >= FETCH_FAIL_NOTIFY_AT:
        msgs.insert(0, f"ℹ️ status.claude.com に再び届くようになりました（{fmt_jst(now)} JST）")

    sent_all = True
    for m in msgs:
        if not send_line(m):
            sent_all = False
    if not msgs:
        log("変化なし（通知なし）")

    append_log(log_rows(prev, new_state, now))
    save_state(new_state)

    # LINE secrets が無い／DRY_RUN のときは「送れなかった」を失敗にしない
    if msgs and not sent_all and os.environ.get("DRY_RUN") != "1" \
            and _clean_secret(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
