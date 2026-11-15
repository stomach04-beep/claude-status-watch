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
  PREVIEW=1                                … 今の未解決の障害を新しく出たとみなして送る（控えは変えない）
  ANTHROPIC_API_KEY                        … 辞書で訳せない件名・公式の説明文を Haiku で訳す（無ければ訳さない）

【安全境界】読むのは公開ステータスページだけ。書くのは state.json と CSV だけ。
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import status_ja  # noqa: E402  英語の件名・部品名を日本語にする変換表（AI 不使用）

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

# 2026-11-15 追加：辞書で訳せない件名と公式の最新の説明文だけ Haiku で日本語にする（利用者承認済み・API クレジット消費）
#   呼ぶのは「発生・悪化の知らせを送る回」だけ・1回の実行で1回まで。部品は release_watch の _ask_claude と同じ物を使う
AI_JOB = "Claude障害の訳"      # 使用ログの「ジョブ」（api_budget の選択肢に無いので「その他」＋備考に入る）
AI_DAILY_CAP = 10              # 1日に呼ぶ上限（暴走の歯止め。超えた回は訳さず送る＝英語は出さない）
AI_SRC_CHARS = 400             # 訳に渡す公式の説明文の長さ（state.json にもこの長さで控える）
AI_OUT_CHARS = 120             # 訳した説明文を通知に載せる長さ


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
            "last_update": latest[:AI_SRC_CHARS],
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


def comps_ja(names: list[str]) -> str:
    """部品名の並びを日本語に（同じ訳が重なれば1つにまとめる）"""
    out: list[str] = []
    for n in names:
        j = status_ja.component_ja(n)
        if j not in out:
            out.append(j)
    return "、".join(out) or "（対象の記載なし）"


def incident_url(iid: str) -> str:
    """障害ごとの公式ページ（スマホのブラウザの翻訳で原文を読める）"""
    return f"{STATUS_PAGE_URL}/incidents/{iid}" if iid else STATUS_PAGE_URL


def recovered_name_ja(inc: dict) -> str:
    """復旧の知らせに出す件名。訳せなければ対象と開始時刻で見分けられるようにする"""
    return status_ja.translate_title(inc.get("name", "")) or \
        f"{comps_ja(inc.get('components') or [])}の障害（{fmt_jst(parse_iso(inc.get('started')))} 開始）"


def incident_change(old: dict | None, inc: dict) -> str | None:
    """前回の控え(old)と今回(inc)を比べて「発生」「悪化」、変化なしなら None（通知・CSV・AI訳で同じ判定を使う）"""
    if old is None:
        return "発生"
    if IMPACT_RANK.get(inc["impact"], 0) > IMPACT_RANK.get(old.get("impact"), 0):
        return "悪化"
    return None


def build_messages(prev: dict, cur: dict, now: datetime,
                   ai_ja: dict | None = None) -> tuple[list[tuple[str, str]], dict]:
    """前回(prev)と今回(cur)を比べ、送るべき LINE 本文（本文, カードの札）のリストと新しい state を返す。
    札は line_flex の level（act=要対応・look=見ておく・info=報告だけ）。本文の語で色が変わらないよう明示する。

    state の形:
      {"is_down": bool, "since": ISO文字列 or None, "incidents": {id: {impact,...}},
       "degraded": [...], "fetch_fail": int, "checked_at": ISO}
    """
    msgs: list[tuple[str, str]] = []
    prev_incs: dict = prev.get("incidents", {}) or {}
    cur_incs: dict = cur["incidents"]

    # ── 新規に出た障害 ／ 影響度が上がった障害 ──
    ai_ja = ai_ja or {}
    for iid, inc in cur_incs.items():
        kind = incident_change(prev_incs.get(iid), inc)
        if kind is None:
            continue  # 変化なし（status が investigating→monitoring になった程度では鳴らさない）
        head = "⚠️ Claude 障害発生" if kind == "発生" else "⚠️ Claude 障害が悪化"
        # 2026-11-15 日本語だけにする（利用者「英語ではなく日本語で」）。英語の原文は state.json と CSV にだけ残す
        #   件名は辞書で訳す → 訳せなければ Haiku の訳 → それも無ければ「訳せませんでした」（英語は出さない）
        ai = ai_ja.get(iid) or {}
        title = status_ja.translate_title(inc["name"])
        if not title and ai.get("title"):
            title = f"{ai['title']}（自動翻訳）"
        comp_line = f"対象: {comps_ja(inc['components'])}"
        lines = [f"{head}（影響度: {IMPACT_JA.get(inc['impact'], inc['impact'])}）"]
        if title:
            lines += [f"内容: {title}", comp_line]
        else:
            # 型の分からない件名は半端に訳さない。通知一覧の1文（先頭2行）で中身が分かるよう対象を2行目に置く
            lines += [comp_line, "内容: 公式の件名が見慣れない型のため訳せませんでした（下のページで確認）"]
            log(f"件名を訳せなかった（辞書に足す候補）: {inc['name']}")
        lines += [f"状況: {status_ja.status_ja(inc['status'])}"]
        if ai.get("update"):
            lines += [f"公式の説明: {ai['update']}（自動翻訳）"]
        lines += [f"開始: {fmt_jst(parse_iso(inc['started']))} 日本時間",
                  f"※ 詳細 {incident_url(iid)}"]
        msgs.append(("\n".join(lines), "act"))

    # ── incident は無いが部品だけ黄色（degraded）になった場合 ──
    prev_deg = set(prev.get("degraded", []) or [])
    cur_deg = set(cur["degraded"])
    if not cur_incs and cur_deg and cur_deg - prev_deg:
        msgs.append((
            "⚠️ Claude 一部機能が不調（正式な障害告知はまだ）\n"
            f"対象: {comps_ja(sorted(cur_deg))}\n"
            f"検知: {fmt_jst(now)} 日本時間\n※ 詳細 {STATUS_PAGE_URL}", "look"))

    # ── 復旧（前回は障害中 → 今回は全部正常）──
    if prev.get("is_down") and not cur["is_down"]:
        since = parse_iso(prev.get("since"))
        # 復旧時刻はページ側の resolved_at のほうが正確だが、
        # summary.json には解決済み incident が残らないので「今回の観測時刻」で代用する
        names = "／".join(recovered_name_ja(v) for v in prev_incs.values()) or "部品の不調"
        msgs.append((
            "✅ Claude 復旧\n"
            f"内容: {names}\n"
            f"発生: {fmt_jst(since)} → 復旧確認: {fmt_jst(now)}（日本時間）\n"
            f"所要: 約{fmt_duration(now - since if since else None)}\n※ 詳細 {STATUS_PAGE_URL}", "info"))

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
# 3.5) Haiku で訳す（飾り。失敗したら {} を返し、呼び出し側は辞書の訳と「訳せませんでした」で送る）
#   障害中は Anthropic の API 自体が落ちていることが多い＝失敗は普通に起きる前提で、通知は止めない
# ─────────────────────────────────────────────
AI_PROMPT = """Claude（Anthropic の AI）の公式の障害情報を、日本の一般ユーザー（獣医師・プログラミング初心者）向けに日本語へ訳してください。
下の <incident> ごとに:
- "title": 件名の訳（30字以内）。<need_title>no</need_title> のときは null
- "update": 公式の説明文の要点（60字以内）。回避策・影響を受けない機能が書いてあれば必ず残す。説明文が空なら null
決まり：
- 製品名・モデル名（Claude、Claude Code、claude.ai、Opus など）と会社名以外は英語を使わない
- 範囲を絞る語（一部の利用者・特定の端末・断続的に など）を落とさない。原文に無いことは書かない
- 返事は JSON だけ。形は {"番号": {"title": "...", "update": "..."}, ...}

"""


def ai_translate(prev: dict, cur: dict, counter: dict, now: datetime) -> dict:
    """発生・悪化として知らせる障害について {incident_id: {"title": 訳 or None, "update": 訳 or None}} を返す。
    counter は state.json に持ち越す1日の呼び出し回数（{"date", "calls"}）。呼べない・失敗なら {}"""
    prev_incs = prev.get("incidents", {}) or {}
    targets = []
    for iid, inc in cur["incidents"].items():
        if incident_change(prev_incs.get(iid), inc) is None:
            continue
        need_title = status_ja.translate_title(inc["name"]) is None
        if need_title or inc.get("last_update"):
            targets.append((iid, inc, need_title))
    if not targets:
        return {}
    api_key = _clean_secret(os.environ.get("ANTHROPIC_API_KEY"))
    if not api_key:
        log("AI訳: ANTHROPIC_API_KEY が無いので訳さず送ります")
        return {}
    today = now.astimezone(JST).strftime("%Y-%m-%d")
    if counter.get("date") != today:
        counter["date"], counter["calls"] = today, 0
    if int(counter.get("calls", 0)) >= AI_DAILY_CAP:
        log(f"AI訳: 今日はすでに{AI_DAILY_CAP}回呼んだので訳さず送ります（上限）")
        return {}
    try:  # 新版の見張りと同じ呼び出し部品・予算の見張りを使う（同じ処理を2か所に書かない）
        from release_watch.release_watch import _ask_claude, SUMMARY_MODEL, SUMMARY_TIMEOUT_SEC, JA_CHARS
        import api_budget
    except Exception as e:  # noqa: BLE001
        log(f"AI訳: 部品を読めず訳さず送ります（{e}）")
        return {}
    decision = None
    try:
        decision = api_budget.check_budget(AI_JOB)
        if decision.mode == "stop":
            log(f"AI訳: 予算が「停止」なので訳さず送ります（{decision.reason}）")
            return {}
    except Exception as e:  # noqa: BLE001  予算判定の不具合で通知を落とさない
        log(f"AI訳: 予算判定できず続行（{e}）")

    prompt = AI_PROMPT
    for n, (iid, inc, need_title) in enumerate(targets, 1):
        prompt += (f'<incident n="{n}">\n<need_title>{"yes" if need_title else "no"}</need_title>\n'
                   f"<title>{inc['name']}</title>\n<update>{inc.get('last_update', '')}</update>\n</incident>\n")
    counter["calls"] = int(counter.get("calls", 0)) + 1     # 失敗しても1回と数える（失敗の連打も歯止めの対象）
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        res = ex.submit(_ask_claude, api_key, prompt).result(timeout=SUMMARY_TIMEOUT_SEC + 15)
    except FutTimeout:
        log("AI訳: 時間切れ（訳さず送ります）")
        res = None
    finally:
        ex.shutdown(wait=False)
    if not res:
        return {}
    usage = res.get("usage", {})
    try:
        api_budget.record_usage(AI_JOB, SUMMARY_MODEL, 1, int(usage.get("input_tokens", 0)),
                                int(usage.get("output_tokens", 0)), note=f"{len(targets)}件の障害",
                                mode=getattr(decision, "mode", None))
    except Exception as e:  # noqa: BLE001
        log(f"AI訳: 使用ログに書けず（{e}）")
    text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}

    def _ok(s) -> str | None:
        """日本語を含み、固有名詞以外の英語が残っていない訳だけ使う（「日本語だけ」を守る）"""
        if not isinstance(s, str):
            return None
        s = s.strip()[:AI_OUT_CHARS]
        if not s or not JA_CHARS.search(s):
            return None
        left = status_ja.leftover_english(s)
        if left:
            log(f"AI訳: 英語が残るので使わない {left}: {s!r}")
            return None
        return s

    out = {}
    for n, (iid, _inc, need_title) in enumerate(targets, 1):
        row = data.get(str(n)) or {}
        if not isinstance(row, dict):
            continue
        out[iid] = {"title": _ok(row.get("title")) if need_title else None, "update": _ok(row.get("update"))}
    log(f"AI訳: {len(targets)}件を依頼・使えた訳 {sum(bool(v['title']) + bool(v['update']) for v in out.values())}個")
    return out


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
        kind = incident_change(prev_incs.get(iid), inc)
        if kind is None:
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
def _line_messages(text, route, level=None):
    """LINE の messages：文章＋共通のカード（色の帯・横線）。line_flex.py は claud-code の line-accounts/line_flex.py の写し
    （2026-10-19）。部品が読めない・カードが大きすぎるときは文章だけ（通知そのものは落とさない）"""
    import os as _os
    import sys as _sys
    d = _os.path.dirname(_os.path.abspath(__file__))
    for _ in range(3):
        if _os.path.isfile(_os.path.join(d, "line_flex.py")):
            if d not in _sys.path:
                _sys.path.insert(0, d)
            break
        d = _os.path.dirname(d)
    try:
        import line_flex
        return line_flex.messages(text, route, level)
    except Exception:  # noqa: BLE001
        return [{"type": "text", "text": str(text)[:4800]}]


def send_line(text: str, level: str | None = None) -> bool:
    """LINE Messaging API に push。成功なら True。DRY_RUN や未設定なら本文を出して False。
    level はカードの札（None なら本文の語から自動判定）"""
    token = _clean_secret(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
    user_id = _clean_secret(os.environ.get("LINE_USER_ID"))
    if os.environ.get("DRY_RUN") == "1" or not token or not user_id:
        why = "DRY_RUN" if os.environ.get("DRY_RUN") == "1" else "LINE secrets 未設定"
        log(f"（{why}のため送信せず）本文↓\n{text}")
        return False
    payload = json.dumps({"to": user_id, "messages": _line_messages(text[:4800], "SYSTEM", level)}).encode("utf-8")
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
        ok = send_line(f"🔧 Claude 障害の見張り 配線テスト\n{fmt_jst(now)}（日本時間）に送信。障害の見張りは20分おきに動いています。\n※ 公式ページ {STATUS_PAGE_URL}", "info")
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
            send_line(f"⚠️ Claude の公式の状況ページに{fails}回続けて届きません（{fmt_jst(now)} 日本時間）。\n"
                      f"Claude 本体の障害かは分かりません。\n※ 手で確認 {STATUS_PAGE_URL}", "look")
        return 2  # 取れなかった回は失敗として残す（緑にしない）

    cur = summarize(data)
    log(f"全体status={data.get('status', {}).get('description')} / 未解決障害={len(cur['incidents'])}件 / 不調部品={cur['degraded'] or 'なし'}")

    counter = dict(prev.get("ai_ja") or {})                # 1日の AI 訳の回数（state.json に持ち越す）
    if os.environ.get("PREVIEW") == "1":
        # 試運転：今の未解決の障害を「新しく出た」とみなして文面と訳を確かめる。控え・CSV は変えない
        ai_ja = ai_translate({}, cur, counter, now)
        msgs, _ = build_messages({}, cur, now, ai_ja)
        msgs = [("🔧 試運転（実際の新しい障害ではありません）\n" + m, "info") for m, _lv in msgs] \
            or [("🔧 試運転：いま未解決の障害はありません", "info")]
        ok = all([send_line(m, lv) for m, lv in msgs])
        return 0 if ok or os.environ.get("DRY_RUN") == "1" else 1
    ai_ja = ai_translate(prev, cur, counter, now)
    msgs, new_state = build_messages(prev, cur, now, ai_ja)
    new_state["ai_ja"] = counter
    if prev.get("fetch_fail", 0) >= FETCH_FAIL_NOTIFY_AT:
        msgs.insert(0, (f"ℹ️ Claude の公式の状況ページに再び届くようになりました（{fmt_jst(now)} 日本時間）", "info"))

    sent_all = True
    for m, level in msgs:
        if not send_line(m, level):
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
