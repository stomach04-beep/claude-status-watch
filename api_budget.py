# -*- coding: utf-8 -*-
"""Claude API クレジット（Max 付属 $100/月）の「使用量の記録」と「予算判定」の共通モジュール。

全部のジョブがこの1本を通ることで、
  - どのジョブがいくら使ったかが Notion「APIクレジット使用ログ」に1実行1行で残る
  - 実行前に「今月このまま行くと使いすぎるか」を同じ物差しで判定できる
  - 判定の閾値・料金表・DB の ID を書く場所が1か所になる（二重定義で必ずズレるのを防ぐ）

使い方（スクリプトから）:
    import sys, os
    sys.path.insert(0, os.path.join(<リポジトリの根>, "api-budget"))
    import api_budget

    decision = api_budget.check_budget("YouTube要約")     # 実行前
    if decision.mode == "stop":
        print(decision.reason); sys.exit(0)               # 今日は見送り（失敗ではない）
    model = "claude-haiku-5-5" if decision.mode == "throttle" else "claude-sonnet-5-5"
    ... API を呼ぶ ...
    api_budget.record_usage("YouTube要約", model, calls=1, input_tokens=..., output_tokens=...,
                            mode=decision.mode, note="動画ID xxxx")

使い方（コマンドライン）:
    python api_budget.py --status                 今月の使用量・着地予測・判定を表示
    python api_budget.py --check "YouTube要約"    判定だけ。終了コード 0=通常 3=節約 4=停止
    python api_budget.py --record --job "YouTube要約" --model claude-haiku-5-5 \
        --calls 1 --input 12000 --output 900 --note "テスト"
    python api_budget.py --watch [--dry-run]      毎日の見張り（節約/停止のときと月曜に LINE）

秘密情報:
    NOTION_TOKEN（必須）、LINE_TOKEN_SYSTEM / LINE_USER_ID_SYSTEM（--watch のみ）。
    環境変数 → 無ければ health-watchdog-local/.env 等の既存 .env から読む。値はログに出さない。
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Windows コンソールの cp932 で絵文字・日本語が化けないように
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# 設定（ここが唯一の定義場所。他のスクリプトに同じ値を書かない）
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_ROOT = os.path.dirname(BASE_DIR)

# Notion「APIクレジット使用ログ」（親: ⚙️ 業務・生活自動化ジョブ）
NOTION_DATABASE_ID = "f399102b7e714484b4ac9d33260fd969"       # REST の /databases/{id}/query と親指定に使う
NOTION_DATA_SOURCE_ID = "170ffccc-a5eb-41c2-9d76-1888be2985c7"  # MCP（Claude Code から読むとき）用
NOTION_API_VERSION = "2022-06-28"
NOTION_TIMEOUT_SEC = 30
NOTION_MAX_RETRIES = 2

# 既存の .env を使い回す（キーの置き場を増やさない）。
# git worktree から動かすと親フォルダに .env が無いので、本体チェックアウトも候補に入れる。
_ENV_ROOTS = [WORKSPACE_ROOT, os.path.join(os.path.expanduser("~"), "Desktop", "Claud Code")]
ENV_FILES = [
    os.path.join(root, sub, ".env")
    for root in _ENV_ROOTS
    for sub in ("mf-portfolio-sync", "health-watchdog-local")
]

# 料金表（USD / 100万トークン、[入力, 出力]）。2026-10-10 時点の公式単価。
# Batch API は半額（estimate_cost(batch=True)）。
PRICE_PER_MTOK = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
}
UNKNOWN_MODEL_PRICE = (4.00, 20.00)   # 表に無いモデルは Opus 並みで高めに見積もる（安全側）
# サーバーツール web_search の単価（USD / 1回）。公式 $10 / 1,000回。トークン代とは別に掛かる。
# 使う側は estimate_cost(...) + 回数 × WEB_SEARCH_USD_PER_CALL を record_usage(cost=...) に渡す
WEB_SEARCH_USD_PER_CALL = 0.01

# 予算の物差し
MONTHLY_CREDIT_USD = float(os.environ.get("API_BUDGET_MONTHLY_USD", "100"))
THROTTLE_RATIO = 0.60     # 月末の着地予測がクレジットの60%を超えたら「節約」
STOP_RATIO = 0.90         # 月初からの実績が90%を超えたら「停止」（残りは手動分の余地）
DAILY_STOP_USD = 5.0      # 1日でこれ以上使ったらその日は「停止」（暴走の即時遮断）
DAILY_THROTTLE_USD = 2.0  # 1日でこれ以上使ったら「節約」
MIN_DAYS_FOR_PROJECTION = 3  # 月初の数日は着地予測が荒れるので予測では判定しない

# 使用ログ DB の「ジョブ」選択肢（DB 側と一致させる。無いものは「その他」に落として備考に残す）
JOB_OPTIONS = [
    "JOB-86 AI仕分け", "JOB-12 事実確認", "YouTube要約", "vetdrug解析", "通知差分要約",
    "apply導入", "発火ニュース照合", "決算下読み", "朝ブリーフ", "予算見張り", "AIテクニカル影信号", "その他",
]
MODE_LABEL = {"normal": "通常", "throttle": "節約", "stop": "停止"}
MODE_EXIT_CODE = {"normal": 0, "throttle": 3, "stop": 4}

JST = dt.timezone(dt.timedelta(hours=9))
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


# ---------------------------------------------------------------------------
# 小道具（ログ・秘密情報・時刻）
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    """時刻つきで1行出す（標準出力。Actions のログでも手元でも同じ見え方）。"""
    print("[{}] {}".format(dt.datetime.now(JST).strftime("%H:%M:%S"), msg), flush=True)


def parse_env_file(path: str) -> dict:
    """.env を読む。KEY=VALUE 形式のみ。BOM・引用符・末尾空白を落とす（RULE-22）。"""
    data = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip().strip('"').strip("'").lstrip("\ufeff")
    except Exception as e:
        log(".env の読み取りに失敗: {} ({})".format(path, e))
    return data


def load_secrets() -> dict:
    """環境変数を優先し、無いキーだけ既存の .env から補う。値は一切ログに出さない。"""
    data = {}
    for path in ENV_FILES:
        if os.path.exists(path):
            data.update(parse_env_file(path))
    for key in ("NOTION_TOKEN", "LINE_TOKEN_SYSTEM", "LINE_USER_ID_SYSTEM",
                "LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID"):
        if os.environ.get(key):
            data[key] = os.environ[key].strip().lstrip("\ufeff")
    return data


def now_jst() -> dt.datetime:
    return dt.datetime.now(JST)


def where_running() -> str:
    """実行場所（使用ログの選択肢に合わせる）。"""
    return "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "PC"


# ---------------------------------------------------------------------------
# 料金
# ---------------------------------------------------------------------------

def estimate_cost(model: str, input_tokens: int, output_tokens: int, batch: bool = False) -> float:
    """トークン数から概算の USD を出す。表に無いモデルは高めに見積もる（安全側）。"""
    price = PRICE_PER_MTOK.get(model, UNKNOWN_MODEL_PRICE)
    cost = input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1]
    if batch:
        cost *= 0.5
    return round(cost, 6)


# ---------------------------------------------------------------------------
# Notion REST（requests に依存しない。urllib だけ）
# ---------------------------------------------------------------------------

def _notion_call(method: str, path: str, token: str, body: dict | None = None):
    """Notion API を1回叩く。429/5xx だけ少し待って再試行。戻り値: (JSON or None, エラー文 or None)"""
    url = "https://api.notion.com/v1/" + path
    headers = {
        "Authorization": "Bearer " + token,
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last_err = None
    for attempt in range(NOTION_MAX_RETRIES + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=NOTION_TIMEOUT_SEC) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            last_err = "HTTP {} {}".format(e.code, text)
            if e.code not in (429, 500, 502, 503, 504):
                return None, last_err
        except Exception as e:
            last_err = str(e)
        if attempt < NOTION_MAX_RETRIES:
            time.sleep(2 ** attempt)
    return None, last_err or "通信失敗"


def _rich(text: str) -> dict:
    return {"rich_text": [{"text": {"content": (text or "")[:1900]}}]}


def _num(prop: dict, key: str):
    v = (prop.get(key) or {}).get("number")
    return v if isinstance(v, (int, float)) else 0


def _sel(prop: dict, key: str) -> str:
    s = (prop.get(key) or {}).get("select") or {}
    return s.get("name", "") or ""


# ---------------------------------------------------------------------------
# 使用ログの読み書き
# ---------------------------------------------------------------------------

def fetch_month_rows(token: str, year: int, month: int) -> list:
    """その月の使用ログを全部読む。戻り値: [{"date": date, "cost": float, "job": str, "model": str}, ...]"""
    first_day = dt.date(year, month, 1).isoformat()
    rows = []
    cursor = None
    while True:
        body = {
            "filter": {"property": "日時", "date": {"on_or_after": first_day}},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        res, err = _notion_call("POST", "databases/{}/query".format(NOTION_DATABASE_ID), token, body)
        if err:
            raise RuntimeError("使用ログの読み取りに失敗: " + err)
        for page in res.get("results", []):
            prop = page.get("properties", {})
            start = ((prop.get("日時") or {}).get("date") or {}).get("start") or ""
            if not start:
                continue
            try:
                d = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
                d = d.astimezone(JST).date() if d.tzinfo else d.date()
            except ValueError:
                d = dt.date.fromisoformat(start[:10])
            rows.append({
                "date": d,
                "cost": float(_num(prop, "概算コスト")),
                "job": _sel(prop, "ジョブ") or "その他",
                "model": "".join(p.get("plain_text", "") for p in (prop.get("モデル") or {}).get("rich_text", [])),
            })
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return rows


def record_usage(job: str, model: str, calls: int, input_tokens: int, output_tokens: int,
                 note: str = "", mode: str | None = None, batch: bool = False,
                 token: str | None = None, where: str | None = None,
                 cost: float | None = None, when: dt.datetime | None = None) -> str | None:
    """1実行分を使用ログ DB に1行書く。戻り値: 作ったページの URL（失敗時 None。例外は投げない）。

    記録に失敗しても本体の処理は止めない（記録は見張りのためで、本体の成果物ではない）。
    ただし失敗はログに残す。cost / when は Console の実測から過去分を補填するときだけ使う。
    """
    token = token or load_secrets().get("NOTION_TOKEN", "")
    if not token:
        log("使用ログ: NOTION_TOKEN が無いので記録を飛ばします")
        return None
    if cost is None:
        cost = estimate_cost(model, input_tokens, output_tokens, batch=batch)
    job_name = job if job in JOB_OPTIONS else "その他"
    extra_note = note if job in JOB_OPTIONS else "[{}] {}".format(job, note)
    ts = when or now_jst()
    props = {
        "記録": {"title": [{"text": {"content": "{} {}".format(job, ts.strftime("%Y-%m-%d %H:%M"))}}]},
        "日時": {"date": {"start": ts.isoformat(timespec="seconds")}},
        "ジョブ": {"select": {"name": job_name}},
        "モデル": _rich(model + (" (batch)" if batch else "")),
        "呼び出し回数": {"number": int(calls)},
        "入力トークン": {"number": int(input_tokens)},
        "出力トークン": {"number": int(output_tokens)},
        "概算コスト": {"number": cost},
        "実行場所": {"select": {"name": where or where_running()}},
        "備考": _rich(extra_note),
    }
    if mode in MODE_LABEL:
        props["判定モード"] = {"select": {"name": MODE_LABEL[mode]}}
    body = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props}
    res, err = _notion_call("POST", "pages", token, body)
    if err:
        log("使用ログ: 記録に失敗（本体の処理は続行）: " + err)
        return None
    log("使用ログ: 記録 {} {} 呼び出し{}回 入力{:,} 出力{:,} 概算${:.4f}".format(
        job, model, calls, input_tokens, output_tokens, cost))
    return res.get("url")


# ---------------------------------------------------------------------------
# 予算判定（Notion を読まない純粋な計算。単体テストはここを叩く）
# ---------------------------------------------------------------------------

def summarize(rows: list, today: dt.date) -> dict:
    """月初からの実績を集計し、月末の着地を予測する。

    着地予測 = 月初からの実績 ÷ 経過日数 × 月の日数（単純な日割り）。
    月初の数日は分母が小さく荒れるので、MIN_DAYS_FOR_PROJECTION 未満は予測を None にする。
    """
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    day = today.day
    mtd = sum(r["cost"] for r in rows if r["date"].month == today.month and r["date"].year == today.year)
    today_cost = sum(r["cost"] for r in rows if r["date"] == today)
    by_job: dict = {}
    for r in rows:
        if r["date"].month == today.month and r["date"].year == today.year:
            by_job[r["job"]] = by_job.get(r["job"], 0.0) + r["cost"]
    projected = None
    if day >= MIN_DAYS_FOR_PROJECTION:
        projected = mtd / day * days_in_month
    return {
        "today": today, "days_in_month": days_in_month, "day": day,
        "mtd": round(mtd, 4), "today_cost": round(today_cost, 4),
        "projected": None if projected is None else round(projected, 2),
        "by_job": dict(sorted(by_job.items(), key=lambda kv: -kv[1])),
        "rows": len(rows), "credit": MONTHLY_CREDIT_USD,
    }


def decide_mode(summary: dict) -> tuple:
    """集計から「通常／節約／停止」を決める。戻り値: (mode, 理由の文)

    停止（stop）  : 月の実績が 90% 超 ／ 今日だけで $5 超 ／ 着地予測がクレジット額超
    節約（throttle）: 着地予測が 60% 超 ／ 今日だけで $2 超
    それ以外は通常。
    """
    credit = summary["credit"]
    mtd, today_cost, projected = summary["mtd"], summary["today_cost"], summary["projected"]
    if mtd >= credit * STOP_RATIO:
        return "stop", "今月の実績 ${:.2f} がクレジットの{:.0%}（${:.0f}）に達した".format(mtd, STOP_RATIO, credit * STOP_RATIO)
    if today_cost >= DAILY_STOP_USD:
        return "stop", "今日だけで ${:.2f} 使った（1日の上限 ${:.0f}）".format(today_cost, DAILY_STOP_USD)
    if projected is not None and projected >= credit:
        return "stop", "このままだと月末に ${:.0f}（クレジット ${:.0f} 超え）".format(projected, credit)
    if projected is not None and projected >= credit * THROTTLE_RATIO:
        return "throttle", "このままだと月末に ${:.0f}（クレジットの{:.0%}超え）".format(projected, THROTTLE_RATIO)
    if today_cost >= DAILY_THROTTLE_USD:
        return "throttle", "今日だけで ${:.2f} 使った（節約に入る目安 ${:.0f}）".format(today_cost, DAILY_THROTTLE_USD)
    return "normal", "今月 ${:.2f}、着地予測 {}".format(
        mtd, "${:.0f}".format(projected) if projected is not None else "（月初のため未算出）")


class BudgetDecision:
    """check_budget の戻り値。mode は 'normal' / 'throttle' / 'stop'。"""

    def __init__(self, mode: str, reason: str, summary: dict | None, degraded: bool = False):
        self.mode = mode
        self.reason = reason
        self.summary = summary
        self.degraded = degraded   # Notion を読めず判定できなかった（通常扱いだが注意）

    @property
    def label(self) -> str:
        return MODE_LABEL[self.mode]

    def __repr__(self) -> str:
        return "BudgetDecision({}: {})".format(self.label, self.reason)


def check_budget(job: str = "", token: str | None = None) -> BudgetDecision:
    """実行前の判定。Notion を読めないときは「通常」で通す（記録は見張りであって本体ではない）。

    ただし degraded=True を立て、ログにも残す。本当に止めたいときは呼び出し側が
    自分の実行回数上限（max calls）で守る前提。Console の月間上限 $100 が最後の砦。
    """
    token = token or load_secrets().get("NOTION_TOKEN", "")
    if not token:
        log("予算判定: NOTION_TOKEN が無いので判定できず「通常」で続行（degraded）")
        return BudgetDecision("normal", "NOTION_TOKEN 無し", None, degraded=True)
    today = now_jst().date()
    try:
        rows = fetch_month_rows(token, today.year, today.month)
    except Exception as e:
        log("予算判定: 使用ログを読めず「通常」で続行（degraded）: {}".format(e))
        return BudgetDecision("normal", "使用ログ読み取り失敗", None, degraded=True)
    summary = summarize(rows, today)
    mode, reason = decide_mode(summary)
    log("予算判定[{}]: {} — {}".format(job or "-", MODE_LABEL[mode], reason))
    return BudgetDecision(mode, reason, summary)


def throttled_model(decision: BudgetDecision, normal_model: str) -> str:
    """節約モードなら Haiku に落とす（通常は指定どおり）。各ジョブで同じ判断を書かないための小道具。"""
    if decision.mode == "throttle":
        return "claude-haiku-5-5"
    return normal_model


# ---------------------------------------------------------------------------
# Console の実測（Usage & Cost Admin API）。使用ログは「概算」なので、実測と突き合わせて
# 「記録に無い使用（Playground・未登録の経路・単価の違い）」を見つける。
# Admin API キー（sk-ant-admin01-…）が要る。環境変数 ANTHROPIC_ADMIN_KEY → .env の順に探す。無ければ黙って飛ばす。
# ---------------------------------------------------------------------------

COST_API_URL = "https://api.anthropic.com/v1/organizations/cost_report"
RECONCILE_ALERT_USD = 1.0     # 実測が概算よりこれ以上多ければ「記録に無い使用」として知らせる
RECONCILE_ALERT_RATIO = 1.5   # かつ、概算の 1.5 倍を超えたとき（小さい額の丸め誤差で鳴らさない）


def load_admin_key() -> str:
    key = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
    if not key:
        key = load_secrets().get("ANTHROPIC_ADMIN_KEY", "")
    return key.lstrip("\ufeff").strip()


def fetch_actual_cost(admin_key: str, year: int, month: int) -> dict | None:
    """その月の実測コスト（USD）を Cost API から日別に読む。戻り値: {"total": float, "by_day": {date: float}}。失敗は None。

    応答の amount は「最小単位（セント）の10進文字列」なので 100 で割る。ページ送り（next_page）に対応。
    """
    start = dt.datetime(year, month, 1, tzinfo=dt.timezone.utc)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    by_day: dict = {}
    page = None
    for _ in range(20):
        q = "starting_at={}&ending_at={}&bucket_width=1d".format(
            start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if page:
            q += "&page=" + page
        req = urllib.request.Request(COST_API_URL + "?" + q, headers={
            "x-api-key": admin_key, "anthropic-version": "2023-06-01",
            "User-Agent": "api-budget/1.0 (personal)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            log("Console 実測: HTTP {}（Admin API キーの権限か期限を確認）".format(e.code))
            return None
        except Exception as e:
            log("Console 実測: 取得失敗 {}".format(e))
            return None
        for bucket in data.get("data", []):
            day = (bucket.get("starting_at") or "")[:10]
            amt = 0.0
            for r in bucket.get("results", []):
                try:
                    amt += float(r.get("amount") or 0) / 100.0
                except (TypeError, ValueError):
                    pass
            if day:
                by_day[day] = by_day.get(day, 0.0) + amt
        if not data.get("has_more"):
            break
        page = data.get("next_page")
    return {"total": round(sum(by_day.values()), 4), "by_day": by_day}


def reconcile(summary: dict, actual: dict | None) -> tuple:
    """概算（使用ログ）と実測（Console）を突き合わせる。戻り値: (本文の行リスト, 警告するか)"""
    if actual is None:
        return ["Console 実測: 取得できず（Admin API キー未設定なら未照合）"], False
    diff = actual["total"] - summary["mtd"]
    lines = ["Console 実測 ${:.2f}（概算との差 {:+.2f}）".format(actual["total"], diff)]
    alert = diff >= RECONCILE_ALERT_USD and actual["total"] >= summary["mtd"] * RECONCILE_ALERT_RATIO
    if alert:
        lines.append("⚠️ 記録に無い使用がある疑い（Playground・未登録の経路・単価の違い）。Console の /cost で日別を確認")
    return lines, alert


# ---------------------------------------------------------------------------
# 見張り（毎日1回。節約・停止のときと、週1回の生存報告だけ LINE に出す）
# ---------------------------------------------------------------------------

def _line_messages(text, route):
    """LINE の messages：文章＋共通のカード（色の帯・横線。line-accounts/line_flex.py・2026-10-19）。
    部品が読めない・カードが大きすぎるときは文章だけ（通知そのものは落とさない）"""
    import os as _os
    import sys as _sys
    d = _os.path.dirname(_os.path.abspath(__file__))
    for _ in range(5):
        cand = _os.path.join(d, "line-accounts")
        if _os.path.isfile(_os.path.join(cand, "line_flex.py")):
            if cand not in _sys.path:
                _sys.path.insert(0, cand)
            break
        d = _os.path.dirname(d)
    try:
        import line_flex
        return line_flex.messages(text, route)
    except Exception:  # noqa: BLE001
        return [{"type": "text", "text": str(text)[:4800]}]


def send_line(text: str, secrets: dict, dry_run: bool) -> bool:
    """システム見張り用の LINE に送る。戻り値を必ず見る（空トークンで黙って成功扱いにしない）。"""
    if dry_run:
        print("----- DRY_RUN: LINE 本文 -----\n" + text + "\n-----")
        return True
    token = secrets.get("LINE_TOKEN_SYSTEM") or secrets.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = secrets.get("LINE_USER_ID_SYSTEM") or secrets.get("LINE_USER_ID", "")
    if not token or not user_id:
        log("LINE: トークンかユーザーIDが無いので送れません")
        return False
    body = json.dumps({"to": user_id, "messages": _line_messages(text[:4900], "SYSTEM")}).encode("utf-8")
    req = urllib.request.Request(LINE_PUSH_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = resp.status == 200
            log("LINE: 送信 {}".format("成功" if ok else "失敗 HTTP {}".format(resp.status)))
            return ok
    except Exception as e:
        log("LINE: 送信失敗 {}".format(e))
        return False


def format_status(summary: dict, mode: str, reason: str) -> str:
    """--status と LINE 本文の共通書式。"""
    icon = {"normal": "🟢", "throttle": "🟡", "stop": "🔴"}[mode]
    lines = [
        "{} APIクレジット {}（{}月{}日時点）".format(icon, MODE_LABEL[mode], summary["today"].month, summary["today"].day),
        "今月の実績 ${:.2f} / クレジット ${:.0f}".format(summary["mtd"], summary["credit"]),
        "今日 ${:.2f}、着地予測 {}".format(
            summary["today_cost"],
            "${:.2f}".format(summary["projected"]) if summary["projected"] is not None else "未算出（月初）"),
        "判定の理由: " + reason,
    ]
    if summary["by_job"]:
        lines.append("内訳（上位）:")
        for job, cost in list(summary["by_job"].items())[:5]:
            lines.append("  {} ${:.2f}".format(job, cost))
    else:
        lines.append("内訳: 今月はまだ記録なし")
    return "\n".join(lines)


def watch(dry_run: bool = False) -> int:
    """毎日の見張り本体。戻り値は終了コード（0=正常に見張れた、1=見張れなかった）。

    送る条件: 節約/停止のとき（毎日）、通常でも月曜（週1の生存報告）。
    記録が1件も無い月は「ジョブが動いていない」可能性もあるので、月曜の報告に明記する。
    """
    secrets = load_secrets()
    token = secrets.get("NOTION_TOKEN", "")
    if not token:
        log("見張り: NOTION_TOKEN が無い")
        return 1
    today = now_jst().date()
    try:
        rows = fetch_month_rows(token, today.year, today.month)
    except Exception as e:
        log("見張り: 使用ログを読めない: {}".format(e))
        ok = send_line("🔧 [APIクレジット見張り] ⚠️ 使用ログを読めません: {}".format(str(e)[:200]), secrets, dry_run)
        return 0 if ok else 1
    summary = summarize(rows, today)
    mode, reason = decide_mode(summary)
    # Console の実測（Admin API キーがあるときだけ）。実測のほうが大きければ着地予測も実測で引き直す
    actual = None
    admin_key = load_admin_key()
    if admin_key:
        actual = fetch_actual_cost(admin_key, today.year, today.month)
        if actual and actual["total"] > summary["mtd"]:
            summary["mtd"] = round(actual["total"], 4)
            if summary["projected"] is not None:
                summary["projected"] = round(actual["total"] / today.day * summary["days_in_month"], 2)
            mode, reason = decide_mode(summary)
    recon_lines, recon_alert = reconcile(summary, actual) if admin_key else ([], False)
    text = format_status(summary, mode, reason)
    if recon_lines:
        text += "\n" + "\n".join(recon_lines)
    print(text)
    is_monday = today.weekday() == 0
    if mode == "normal" and not is_monday and not recon_alert:
        log("見張り: 通常・平日なので LINE は送らない")
        return 0
    header = "🔧 [APIクレジット見張り] " + (
        "⚠️ 記録に無い使用" if (mode == "normal" and recon_alert) else
        "週次報告" if mode == "normal" else "⚠️ 使いすぎの兆候")
    if mode != "normal":
        header += "\nジョブ側は次の実行からこのモードで動きます（停止=見送り、節約=Haikuに落とす）"
    ok = send_line(header + "\n" + text, secrets, dry_run)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# コマンドライン
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Claude API クレジットの使用量記録と予算判定")
    p.add_argument("--status", action="store_true", help="今月の集計と判定を表示")
    p.add_argument("--check", metavar="JOB", help="判定だけ（終了コード 0=通常 3=節約 4=停止）")
    p.add_argument("--record", action="store_true", help="1実行分を記録する")
    p.add_argument("--watch", action="store_true", help="毎日の見張り（LINE）")
    p.add_argument("--dry-run", action="store_true", help="LINE を送らず本文を表示")
    p.add_argument("--job", default="その他")
    p.add_argument("--model", default="claude-haiku-5-5")
    p.add_argument("--calls", type=int, default=1)
    p.add_argument("--input", type=int, default=0)
    p.add_argument("--output", type=int, default=0)
    p.add_argument("--note", default="")
    p.add_argument("--batch", action="store_true", help="Batch API（半額）として記録")
    p.add_argument("--cost", type=float, default=None, help="概算でなく実測の USD を入れる（補填用）")
    p.add_argument("--when", default=None, help="記録の日時 YYYY-MM-DD[THH:MM]（補填用。既定は今）")
    args = p.parse_args(argv)

    if args.record:
        when = None
        if args.when:
            when = dt.datetime.fromisoformat(args.when)
            if when.tzinfo is None:
                when = when.replace(tzinfo=JST)
        url = record_usage(args.job, args.model, args.calls, args.input, args.output,
                           note=args.note, batch=args.batch, cost=args.cost, when=when)
        print(url or "記録失敗")
        return 0 if url else 1
    if args.watch:
        return watch(dry_run=args.dry_run)
    if args.check is not None:
        d = check_budget(args.check)
        print(d.reason)
        return MODE_EXIT_CODE[d.mode]
    # 既定は --status（Admin API キーがあれば Console の実測も添える）
    d = check_budget("status")
    if d.summary:
        print(format_status(d.summary, d.mode, d.reason))
        admin_key = load_admin_key()
        if admin_key:
            actual = fetch_actual_cost(admin_key, d.summary["today"].year, d.summary["today"].month)
            print("\n".join(reconcile(d.summary, actual)[0]))
        else:
            print("Console 実測: ANTHROPIC_ADMIN_KEY 未設定のため未照合（Console > 組織 > Admin keys で発行し .env と Secret に入れると照合する）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
