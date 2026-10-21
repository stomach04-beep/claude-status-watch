# -*- coding: utf-8 -*-
"""
release_watch.py ― Claude の「新しい版」と「モデルの更新」を見張り、出たときだけ LINE（システム見張り）に知らせる。

【何をするスクリプトか】（VBAでいうと「4つのWebページの先頭行を前回の控えと比べ、違ったらMsgBox」）
  見張る先（どれも公開ページ・ログイン不要・AI不使用）
    1. Claude Code 本体の版       … npm の @anthropic-ai/claude-code の latest ＋ CHANGELOG.md の箇条書き
    2. モデルの顔ぶれ             … モデル一覧ページの表「Claude API ID」の行。増えた＝新モデル、消えた＝一覧から外れた
    3. API（Claude Platform）の更新 … リリースノートの一番新しい日付の節（新モデル発表・値下げ・廃止予定もここに出る）
    4. Claude アプリ（デスクトップ・Web・スマホ）の更新 … ヘルプセンターのリリースノートの一番新しい日付
  前回の控え（state.json）と比べ、変わったものだけを1枚のカードにまとめて送る。変化が無ければ黙る。

【初回】控えが無い見張り先は「今の値を控えるだけ」で知らせない（一度に4通の「新版」が飛ばないように）。
【取得失敗】その見張り先は前回の値のまま（失敗を「変化」と数えない）。連続 FETCH_FAIL_NOTIFY_AT 回で1回だけ知らせ、
           戻ったときも1回知らせる。
【環境変数】
  LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID … 通知先（無ければ本文を出すだけ）
  DRY_RUN=1                                … 送信せず本文を標準出力（手元で試すときは必ずこれ）
  FORCE_TEST=1                             … 状態に関係なく「今の版」の一覧を1通送る（配線確認用。控えは更新しない）
  PREVIEW=1                                … 各見張り先の「1つ前の版→最新」を変化とみなして要約つきで1通作る（要約の試運転。控えは更新しない）
  ANTHROPIC_API_KEY                        … 日本語の要約に使う（無ければ英語の箇条書きのまま送る）
  NOTION_TOKEN                             … API クレジットの予算判定と使用ログ（api_budget.py。無くても要約は動く）

【日本語の要約】2026-10-21 追加（ユーザー「内容を要約した上で通知してほしい。無料で」→ Max 付属の API クレジットで Haiku に決定）
  通知が出る回だけ、英語の箇条書きを Haiku 5.5 に渡して ■ のまとまりごとに日本語2〜3行へまとめる（1回の通知につき呼び出し1回）。
  要約は飾り：鍵なし・失敗・時間切れ・日本語が返らない・1日の上限（SUMMARY_DAILY_CAP）超え・予算「停止」のときは、
  今までどおり英語の箇条書きで送る。要約の成否で終了コードも送信も変えない。

【安全境界】読むのは公開ページだけ。書くのは release_watch/state.json だけ（＋使用ログ DB に1行）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ─────────────────────────────────────────────
# 設定（URL・しきい値はここだけ＝単一の真実の源）
# ─────────────────────────────────────────────
NPM_URL = "https://registry.npmjs.org/@anthropic-ai/claude-code/latest"
CHANGELOG_URL = "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
CHANGELOG_PAGE = "https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md"
MODELS_URL = "https://platform.claude.com/docs/en/models/overview.md"
MODELS_PAGE = "https://platform.claude.com/docs/en/models/overview"
API_NOTES_URL = "https://platform.claude.com/docs/en/release-notes/overview.md"
API_NOTES_PAGE = "https://platform.claude.com/docs/en/release-notes/overview"
APP_NOTES_URL = "https://platform.claude.com/docs/en/release-notes/claude-apps.md"   # ヘルプセンターへ転送される
APP_NOTES_PAGE = "https://support.claude.com/en/articles/12138966-release-notes"
LINE_API_URL = "https://api.line.me/v2/bot/message/push"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")

FETCH_RETRY = 3            # 1つの見張り先の取得リトライ回数
FETCH_TIMEOUT_SEC = 20     # 1回あたりのタイムアウト
FETCH_FAIL_NOTIFY_AT = 6   # 毎時なので連続6回（約6時間）取れなかったら1回だけ知らせる
MAX_BULLETS = 4            # 1つの更新に載せる英語の箇条書きの数
MAX_BULLET_CHARS = 110     # 箇条書き1つの長さ（長いと LINE のカードが読みにくい）

# 日本語の要約（Claude API）
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
SUMMARY_MODEL = "claude-haiku-5-5"
SUMMARY_JOB = "Claude新版の要約"      # 使用ログの「ジョブ」（api_budget の選択肢に無いので「その他」＋備考に入る）
SUMMARY_DAILY_CAP = 5        # 1日に要約を呼ぶ上限（暴走の歯止め。超えた分は英語のまま送る）
SUMMARY_MAX_TOKENS = 1500    # 返事の長さの上限（要約は短いが、切れて JSON が壊れないよう余裕を持たせる）
SUMMARY_TIMEOUT_SEC = 60     # 1回の呼び出しの絶対上限（応答が返らないまま待ち続けない）
SUMMARY_SRC_BULLETS = 15     # 要約に渡す英語の箇条書きの数（1つの版あたり）
JA_CHARS = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")   # ひらがな・カタカナ・漢字

SOURCE_JA = {
    "cli": "Claude Code 本体",
    "models": "モデルの顔ぶれ",
    "api": "API の更新情報",
    "app": "Claude アプリの更新情報",
}

JST = timezone(timedelta(hours=9))
UA = "claude-release-watch/1.0 (+https://github.com/stomach04-beep/claude-status-watch)"


def log(msg: str) -> None:
    """時刻つきで標準出力に出す（Actions のログで追えるように）"""
    print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _clean_secret(v: str | None) -> str:
    """Secret の前後の空白・BOM を落とす（貼り付け時に混ざる罠）"""
    return (v or "").strip().lstrip("﻿")


def fetch(url: str) -> str | None:
    """ページを文字列で取る。失敗したらリトライし、それでも駄目なら None"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for i in range(1, FETCH_RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as r:
                return r.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            log(f"取得失敗（{i}回目）{url}: {e}")
        time.sleep(3 * i)
    return None


def short(s: str, n: int = MAX_BULLET_CHARS) -> str:
    """リンク記法・強調記号を外して n 文字に切る"""
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)        # [文字](URL) → 文字
    s = re.sub(r"<[^>]+>", "", s).replace("**", "").replace("`", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ─────────────────────────────────────────────
# 1) 見張り先を読む（どれも「新しい順の節のリスト」か値を返す。読めなければ None）
#    節 = (見出し, [箇条書き])。見出しは版番号か日付で、前回の控えと比べる鍵になる
# ─────────────────────────────────────────────
def read_cli() -> dict | None:
    """Claude Code 本体：npm の latest を正とし、CHANGELOG.md の節から箇条書きを取る"""
    raw = fetch(NPM_URL)
    try:
        ver = json.loads(raw or "")["version"]
    except (ValueError, KeyError, TypeError):
        return None
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(ver)):
        return None
    sections: list[tuple[str, list[str]]] = []
    md = fetch(CHANGELOG_URL) or ""            # 箇条書きは飾り。取れなくても版の通知は出す
    for m in re.finditer(r"^## (\d+\.\d+\.\d+)\s*$(.*?)(?=^## |\Z)", md, re.M | re.S):
        bullets = [l[2:] for l in m.group(2).splitlines() if l.startswith("- ")]
        sections.append((m.group(1), bullets))
    return {"value": ver, "sections": sections}


def read_models() -> dict | None:
    """モデル一覧ページの表の「Claude API ID」行から、いま並んでいるモデルIDと名前を取る"""
    md = fetch(MODELS_URL)
    if not md:
        return None
    rows = [l for l in md.splitlines() if l.startswith("|")]
    head = next((r for r in rows if r.lstrip("| ").startswith("Feature")), None)
    idrow = next((r for r in rows if r.lstrip("| ").startswith("Claude API ID")), None)
    if not head or not idrow:
        return None                            # 表の形が変わった＝読めない（変化とは数えない）
    names = [c.strip() for c in head.strip("|").split("|")][1:]
    ids = [re.sub(r"[`\s]", "", c) for c in idrow.strip("|").split("|")][1:]
    models = {i: n for i, n in zip(ids, names) if re.fullmatch(r"claude-[a-z0-9.-]+", i)}
    return {"value": sorted(models), "names": models} if models else None


def read_api_notes() -> dict | None:
    """API のリリースノート：「### October 15, 2026」の節ごとに箇条書きを取る"""
    md = fetch(API_NOTES_URL)
    if not md:
        return None
    sections = []
    for m in re.finditer(r"^### ([A-Z][a-z]+ \d{1,2}, \d{4})\s*$(.*?)(?=^### |\Z)", md, re.M | re.S):
        bullets = [l.strip()[2:] for l in m.group(2).splitlines() if l.strip()[:2] in ("* ", "- ")]
        sections.append((m.group(1), bullets))
    return {"value": sections[0][0], "sections": sections} if sections else None


def read_app_notes() -> dict | None:
    """Claude アプリのリリースノート（HTML）：<h3>日付</h3> の節ごとに、太字の小見出しを箇条書きとして取る"""
    html = fetch(APP_NOTES_URL)
    if not html:
        return None
    sections = []
    pat = r"<h3[^>]*>\s*([A-Z][a-z]+ \d{1,2}, \d{4})\s*</h3>(.*?)(?=<h[23][ >]|\Z)"
    for m in re.finditer(pat, html, re.S):
        bullets = [short(b) for b in re.findall(r"<p><b>(.*?)</b></p>", m.group(2), re.S)]
        sections.append((m.group(1), [b for b in bullets if b]))
    return {"value": sections[0][0], "sections": sections} if sections else None


READERS = {"cli": read_cli, "models": read_models, "api": read_api_notes, "app": read_app_notes}


# API の更新情報は開発者向けの細かい話が多いので、モデルの話（発表・値下げ・廃止など）だけ知らせる
MODEL_WORDS = re.compile(r"Claude (Fable|Opus|Sonnet|Haiku|Mythos)\b|claude-[a-z]+-\d", re.I)
MODEL_EVENTS = re.compile(r"launch|releas|introduc|availab|retir|deprecat|price|pricing|discontinu", re.I)


def is_model_news(bullet: str) -> bool:
    return bool(MODEL_WORDS.search(bullet) and MODEL_EVENTS.search(bullet))


# ─────────────────────────────────────────────
# 2) 前回の控えと比べる（新しい節を前回の鍵の手前まで集める）
# ─────────────────────────────────────────────
def newer_sections(sections: list, prev_key: str, limit: int = 3) -> list:
    """新しい順の節から、前回の鍵より新しいものを最大 limit 個返す。前回の鍵が見当たらなければ先頭1個だけ"""
    keys = [k for k, _ in sections]
    if prev_key in keys:
        return sections[: keys.index(prev_key)][:limit]
    return sections[:1]


def diff_source(key: str, prev_val, cur: dict) -> list[str]:
    """1つの見張り先について、通知に載せる行（■見出し＋・箇条書き）を返す。変化なしなら空"""
    val = cur["value"]
    if val == prev_val:
        return []
    out: list[str] = []
    if key == "cli":
        out.append(f"■ Claude Code 本体 {prev_val} → {val}")
        secs = newer_sections(cur["sections"], prev_val) or [(val, [])]
        for ver, bullets in secs:
            if len(secs) > 1:
                out.append(f"v{ver}")          # 版が2つ以上たまったときだけ版ごとの見出しを付ける
            out += [f"・{short(b)}" for b in bullets[:MAX_BULLETS]]
            if len(bullets) > MAX_BULLETS:
                out.append(f"・ほか{len(bullets) - MAX_BULLETS}件")
        out.append(f"※ 全文 {CHANGELOG_PAGE}")
    elif key == "models":
        names = cur.get("names", {})
        added = [m for m in val if m not in (prev_val or [])]
        removed = [m for m in (prev_val or []) if m not in val]
        out.append("■ モデルの顔ぶれが変わりました")
        out += [f"・🆕 {names.get(m, m)}（{m}）" for m in added]
        out += [f"・一覧から外れた：{m}" for m in removed]
        out.append(f"※ {MODELS_PAGE}")
    elif key == "api":
        hits = [(d, [b for b in bs if is_model_news(b)]) for d, bs in newer_sections(cur["sections"], prev_val, limit=6)]
        hits = [(d, bs) for d, bs in hits if bs]
        if not hits:
            return []                          # モデルの話が無い更新は黙って控えだけ進める
        out.append("■ API の更新（モデル関連）")
        for d, bs in hits:
            out += [f"・{d}：{short(b)}" for b in bs[:MAX_BULLETS]]
        out.append(f"※ {API_NOTES_PAGE}")
    elif key == "app":
        out.append("■ Claude アプリの更新")
        for d, bs in newer_sections(cur["sections"], prev_val):
            out += [f"・{d}：{b}" for b in bs[:MAX_BULLETS]] or [f"・{d}"]
        out.append(f"※ {APP_NOTES_PAGE}")
    return out


def source_material(key: str, prev_val, cur: dict) -> str:
    """要約に渡す英語の原文（カード用に4件・110文字で切る前の箇条書き）。diff_source と同じ節の選び方をする"""
    if key == "models":
        return ""                              # 顔ぶれの増減はカードの行そのもので足りる
    if key == "cli":
        secs = newer_sections(cur["sections"], prev_val)
    elif key == "api":
        secs = [(d, [b for b in bs if is_model_news(b)])
                for d, bs in newer_sections(cur["sections"], prev_val, limit=6)]
    else:
        secs = newer_sections(cur["sections"], prev_val)
    parts = []
    for head, bullets in secs:
        if bullets:
            parts.append(f"[{head}]\n" + "\n".join(f"- {short(b, 400)}" for b in bullets[:SUMMARY_SRC_BULLETS]))
    return "\n".join(parts)


# ─────────────────────────────────────────────
# 2.5) 日本語の要約（飾り。失敗したら None を返し、呼び出し側は英語のまま送る）
# ─────────────────────────────────────────────
SUMMARY_PROMPT = """あなたは Claude（Anthropic の AI）の更新情報を、日本の一般ユーザー（獣医師・プログラミング初心者）向けに短く紹介する係です。
下の <block> ごとに、何が変わったかを日本語で2〜3行にまとめてください。

決まり：
- 1行は40字以内。専門用語は平易な言い換えにし、コマンド名・機能名・モデル名は原文のまま残してよい
- 利用者に関係の大きいもの（新モデル・新機能・料金・廃止・よく使う機能の不具合修正）を優先し、細かい修正は「ほか細かな修正」とまとめる
- 原文に無いことは書かない。推測で補わない
- 返事は JSON だけ。形は {"ブロック番号": ["1行目", "2行目"], ...}（行頭に「・」は付けない）

"""


def _ask_claude(api_key: str, prompt: str) -> dict | None:
    """Claude API を1回呼ぶ。戻り値は API の JSON（失敗なら None）"""
    body = json.dumps({
        "model": SUMMARY_MODEL,
        "max_tokens": SUMMARY_MAX_TOKENS,
        # Haiku 5.5 は既定で「考える」が自動でオンになり、出力の上限を考える側で使い切って本文が空になる
        # （2026-10-22 実測：出力800トークン・本文0文字）。要約は考える必要がないのでオフにする（effort 既定 medium では可）
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(ANTHROPIC_URL, data=body, method="POST", headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=SUMMARY_TIMEOUT_SEC) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"要約: API が HTTP {e.code} {e.read()[:200]!r}")
    except Exception as e:  # noqa: BLE001
        log(f"要約: API 呼び出し失敗 {e}")
    return None


def summarize_ja(blocks: list[list[str]], materials: list[str], state: dict, now: datetime) -> list | None:
    """■ のまとまりごとに日本語の要約行を返す（[行のリスト or None, ...]）。全体が使えなければ None"""
    api_key = _clean_secret(os.environ.get("ANTHROPIC_API_KEY"))
    if not api_key:
        log("要約: ANTHROPIC_API_KEY が無いので英語のまま送ります")
        return None
    # 暴走の歯止め：1日の呼び出し回数を控え（state.json）で数える
    ent = state.setdefault("summary", {})
    today = now.strftime("%Y-%m-%d")
    if ent.get("date") != today:
        ent["date"], ent["calls"] = today, 0
    if int(ent.get("calls", 0)) >= SUMMARY_DAILY_CAP:
        log(f"要約: 今日はすでに{SUMMARY_DAILY_CAP}回呼んだので英語のまま送ります（上限）")
        return None
    # API クレジットの予算判定（api_budget.py は claud-code の api-budget/api_budget.py の写し・リポジトリ直下）
    budget = None
    try:
        root = os.path.dirname(BASE_DIR)
        if root not in sys.path:
            sys.path.insert(0, root)
        import api_budget
        budget = api_budget
        decision = api_budget.check_budget(SUMMARY_JOB)
        if decision.mode == "stop":
            log(f"要約: 予算が「停止」なので英語のまま送ります（{decision.reason}）")
            return None
    except Exception as e:  # noqa: BLE001  予算判定の不具合で通知を落とさない
        log(f"要約: 予算判定できず続行（{e}）")
        decision = None

    prompt = SUMMARY_PROMPT
    for i, (lines, mat) in enumerate(zip(blocks, materials), 1):
        prompt += f'<block n="{i}">\n' + "\n".join(lines) + ("\n原文:\n" + mat if mat else "") + "\n</block>\n"

    ent["calls"] = int(ent.get("calls", 0)) + 1     # 失敗しても1回と数える（失敗の連打も歯止めの対象）
    # urlopen の timeout は1回の読み取りの上限なので、別スレッドで呼んで全体の上限をかける
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        res = ex.submit(_ask_claude, api_key, prompt).result(timeout=SUMMARY_TIMEOUT_SEC + 15)
    except FutTimeout:
        log("要約: 時間切れ（英語のまま送ります）")
        res = None
    finally:
        ex.shutdown(wait=False)
    if not res:
        return None

    if res.get("stop_reason") not in ("end_turn", "stop_sequence"):
        log(f"要約: 返事が途中で止まった（stop_reason={res.get('stop_reason')}）")
    usage = res.get("usage", {})
    if budget is not None:
        budget.record_usage(SUMMARY_JOB, SUMMARY_MODEL, 1, int(usage.get("input_tokens", 0)),
                            int(usage.get("output_tokens", 0)), note=f"{len(blocks)}件のまとまり",
                            mode=getattr(decision, "mode", None))
    text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)                 # 前後に説明文や ``` が付いても JSON 部分だけ取る
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    out = []
    for i in range(1, len(blocks) + 1):
        rows = data.get(str(i))
        rows = [short(str(r), 80).lstrip("・ ") for r in rows] if isinstance(rows, list) else []
        rows = [r for r in rows if r][:4]
        out.append(rows if rows and all(JA_CHARS.search(r) for r in rows) else None)   # 日本語でなければ使わない
    if not any(out):
        log(f"要約: 使える日本語が返らなかった（英語のまま送ります）返事の先頭: {text[:200]!r}")
        return None
    return out


def merge_summary(block: list[str], summary: list[str] | None) -> list[str]:
    """1つの ■ のまとまりに要約を差し込む。■見出しと ※URL は残し、英語の箇条書きを日本語の要約に置き換える"""
    if not summary:
        return block
    return [block[0], *[f"・{s}" for s in summary], *[l for l in block if l.startswith("※")]]


# ─────────────────────────────────────────────
# 3) 控えの読み書き
# ─────────────────────────────────────────────
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ─────────────────────────────────────────────
# 4) LINE 送信（戻り値を必ず見る＝虚偽ログ防止）
# ─────────────────────────────────────────────
def _line_messages(text: str) -> list[dict]:
    """共通のカード（line_flex.py は claud-code の line-accounts/line_flex.py の写し・リポジトリ直下）。
    新版のお知らせは「報告だけ」の色にする（英語の本文に引きずられて要対応の色にならないよう level を渡す）"""
    root = os.path.dirname(BASE_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import line_flex
        return line_flex.messages(text, "SYSTEM", level="info")
    except Exception:  # noqa: BLE001  カードの失敗で通知を落とさない
        return [{"type": "text", "text": text[:4800]}]


def send_line(text: str) -> bool:
    """LINE に push。成功なら True。DRY_RUN・鍵なしなら本文を出して False"""
    token = _clean_secret(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
    user_id = _clean_secret(os.environ.get("LINE_USER_ID"))
    if os.environ.get("DRY_RUN") == "1" or not token or not user_id:
        why = "DRY_RUN" if os.environ.get("DRY_RUN") == "1" else "LINE secrets 未設定"
        log(f"（{why}のため送信せず）本文↓\n{text}")
        return False
    payload = json.dumps({"to": user_id, "messages": _line_messages(text[:4800])}).encode("utf-8")
    req = urllib.request.Request(
        LINE_API_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log(f"LINE送信: HTTP {r.status}")
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        log(f"LINE送信失敗: HTTP {e.code} {e.read()[:200]!r}")
    except Exception as e:  # noqa: BLE001
        log(f"LINE送信失敗: {e}")
    return False


# ─────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────
def main() -> int:
    now = datetime.now(JST)
    prev = load_state()
    state = json.loads(json.dumps(prev))       # 書き換え用の写し（変化が無ければ保存しない）
    lines: list[str] = []                      # 通知の本文（■見出しごと）
    notes: list[str] = []                      # 取れない・戻った の知らせ
    any_fail = False

    current = {}
    for key, reader in READERS.items():
        try:
            cur = reader()
        except Exception as e:  # noqa: BLE001  1つの見張り先の不具合で全体を止めない
            log(f"{key} の読み取りで例外: {e}")
            cur = None
        ent = state.setdefault(key, {})
        if cur is None:
            any_fail = True
            ent["fail"] = int(ent.get("fail", 0)) + 1
            log(f"{SOURCE_JA[key]}：取れない（連続{ent['fail']}回）")
            if ent["fail"] == FETCH_FAIL_NOTIFY_AT:
                notes.append(f"・⚠ {SOURCE_JA[key]}が{ent['fail']}回続けて読めません（ページの形が変わった可能性）")
            continue
        if int(ent.get("fail", 0)) >= FETCH_FAIL_NOTIFY_AT:
            notes.append(f"・{SOURCE_JA[key]}がまた読めるようになりました")
        ent["fail"] = 0
        current[key] = cur
        log(f"{SOURCE_JA[key]}：{cur['value']}")

    if os.environ.get("FORCE_TEST") == "1":
        body = [f"・{SOURCE_JA[k]}：{', '.join(c['value']) if isinstance(c['value'], list) else c['value']}"
                for k, c in current.items()]
        ok = send_line("\n".join([f"【Claude 新版の見張り】配線テスト {now:%m/%d %H:%M}", "いま控えている版", *body]))
        return 0 if ok or os.environ.get("DRY_RUN") == "1" else 1

    preview = os.environ.get("PREVIEW") == "1"
    blocks: list[list[str]] = []               # ■ のまとまりごとの行
    materials: list[str] = []                  # まとまりごとの要約用の原文
    for key, cur in current.items():
        ent = state[key]
        prev_val = ent.get("value")
        if preview:                            # 試運転：1つ前の版（節）から変わったことにする
            secs = cur.get("sections") or []
            prev_val = secs[1][0] if len(secs) > 1 else None
            if prev_val is None:
                continue
        elif "value" not in ent:               # 初回は控えるだけ（知らせない）
            log(f"{SOURCE_JA[key]}：初回なので控えるだけ")
            ent["value"] = cur["value"]
            continue
        block = diff_source(key, prev_val, cur)
        if block:
            blocks.append(block)
            materials.append(source_material(key, prev_val, cur))
        ent["value"] = cur["value"]

    if blocks:
        summaries = summarize_ja(blocks, materials, state, now) or [None] * len(blocks)
        for block, summ in zip(blocks, summaries):
            lines += merge_summary(block, summ)

    if preview:
        text = "\n".join([f"🆕【Claude 新版】要約の試運転 {now:%m/%d %H:%M}", "", *lines]) if lines else "（試運転：材料なし）"
        ok = send_line(text)
        return 0 if ok or os.environ.get("DRY_RUN") == "1" else 1

    if lines or notes:
        level_mark = "🆕" if lines else "⚠"
        text = "\n".join([f"{level_mark}【Claude 新版】{now:%m/%d %H:%M}",
                          "新しい版・モデルの更新が出ました" if lines else "見張り先が読めません",
                          "", *lines, *(([""] + notes) if notes else [])])
        if not send_line(text):
            if os.environ.get("DRY_RUN") == "1" or not _clean_secret(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")):
                return 0                       # 試運転・鍵なし：控えは進めない（本番で改めて知らせる）
            return 3                           # 送れなかった：控えを進めず次の回でもう一度送る
    else:
        log("変化なし（通知なし）")

    if state != prev:
        save_state(state)
        log("控えを更新しました")
    return 2 if any_fail and not current else 0   # 全部取れなかった回だけ失敗にする（緑にしない）


if __name__ == "__main__":
    sys.exit(main())
