# -*- coding: utf-8 -*-
"""LINE の Flex メッセージ（表・横棒・見出しの帯のカード）を組む共通の部品

2026-10-19 ユーザー「まだわかりにくい。トークン消費の週次グラフはわかりやすい。必要に応じて表みたいに」→ Fable 相談。
手本は token-usage-monitor/token_usage_weekly.py の build_flex（画像を使わず LINE の部品だけで描く）。
全部の通知を同じ骨組みにして、目が1つの配置を覚えられるようにする：
  ① 見出しの帯 … アカウントの色（くらし緑・投資青・IPO紫・見張り灰）＋「何をするか」の札（🔴要対応／🟡見ておく／🟢報告だけ）
  ② 結論1つ   … 大きい字の1文か数字（例「きょうの合計 −52.8万円」）
  ③ 中身     … 表（2〜4列）か横棒
  ④ 注記     … 小さい灰色の字（「模擬・注文なし」など）
札（level）と色は送り手が判定して渡す（ここでは判定し直さない＝同じ判定を2か所に書かない）。

送り方：文章（検索・コピー用）と Flex を1回の push に2つ入れる＝送信数は1通のまま。
Flex の形式で断られた（HTTP 400）ときだけ文章だけで送り直す。通知一覧に出る altText は文章の1行目と同じにする。

LINE の Flex の罠（守ること）：幅0%の箱は断られる（最低1%）／altText は400字まで／日本語は wrap を付けないと「…」で切れる／
色は #RRGGBB／1回の push は5メッセージまで。

使う人：auto-trading-lab/papertrade/notify.py・linekeeper/keeper.py・forward-checks/yen_ledger.py・health-watchdog-local（第1弾）
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

ROUTE_COLOR = {"LIFE": "#1A7F37", "INVEST": "#2563EB", "IPO": "#7C3AED", "SYSTEM": "#4B5563"}
LEVELS = {"act": "🔴 要対応", "look": "🟡 見ておく", "info": "🟢 報告だけ"}
PLUS, MINUS, GRAY, TEXT = "#1A7F37", "#D93025", "#888888", "#333333"
RULE = "#D1D5DB"      # 表の横線の色（2026-10-19 ユーザー「横線があったほうが見やすい」）


# ───── 部品 ─────
def _text(text: str, size: str = "xs", color: str = TEXT, bold: bool = False, align: str | None = None,
          flex: int | None = None, margin: str | None = None) -> dict:
    t = {"type": "text", "text": str(text) if str(text) != "" else " ", "size": size, "color": color, "wrap": True}
    if bold:
        t["weight"] = "bold"
    if align:
        t["align"] = align
    if flex is not None:
        t["flex"] = flex
    if margin:
        t["margin"] = margin
    return t


def header(title: str, route: str = "INVEST", level: str = "info", sub: str = "") -> dict:
    """① 見出しの帯（アカウントの色の帯に白い字）。sub は日付など"""
    contents = [
        {"type": "box", "layout": "horizontal", "contents": [
            _text(title, "md", "#FFFFFF", bold=True, flex=5),
            _text(LEVELS.get(level, level), "xs", "#FFFFFF", align="end", flex=2)]},
    ]
    if sub:
        contents.append(_text(sub, "xs", "#E5E7EB"))
    return {"type": "box", "layout": "vertical", "backgroundColor": ROUTE_COLOR.get(route, ROUTE_COLOR["INVEST"]),
            "paddingAll": "12px", "spacing": "xs", "contents": contents}


def kpi(text: str, color: str = TEXT) -> dict:
    """② 結論1つ（大きい字）"""
    return _text(text, "lg", color, bold=True)


def section(title: str) -> list[dict]:
    """小見出し（区切り線つき）"""
    return [{"type": "separator", "margin": "md"}, _text(title, "sm", TEXT, bold=True, margin="md")]


def table(cols: list[tuple[str, int, str]], rows: list[list], colors: list[list[str | None]] | None = None) -> list[dict]:
    """③ 表。cols は [(見出し, 幅の比率, "start"|"end"), ...]。rows は文字の2次元。colors は同じ形で字の色（None は既定）"""
    out = [{"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
        _text(h, "xxs", GRAY, bold=True, align=al, flex=w) for h, w, al in cols]}]
    out.append(rule(strong=True))                                 # 見出しの下は濃いめの線
    for i, r in enumerate(rows):
        if i:
            out.append(rule())                                      # 行と行のあいだに横線
        cells = []
        for j, (h, w, al) in enumerate(cols):
            c = (colors[i][j] if colors and colors[i] and j < len(colors[i]) else None) or TEXT
            cells.append(_text(r[j] if j < len(r) else "", "xs", c, align=al, flex=w))
        out.append({"type": "box", "layout": "horizontal", "spacing": "sm", "contents": cells})
    return out


def bars(rows: list[tuple[str, float, str]], label_flex: int = 4) -> list[dict]:
    """③ 横棒（正は緑・負は赤。長さは絶対値の最大を100%）。rows は [(ラベル, 値, 値の文字), ...]"""
    vmax = max((abs(v) for _l, v, _t in rows), default=0) or 1
    out = []
    for k, (label, v, vt) in enumerate(rows):
        if k:
            out.append(rule())                                      # 棒と棒のあいだにも横線
        width = min(100, max(1, round(abs(v) / vmax * 100)))     # 0% の箱は LINE に断られる
        color = PLUS if v > 0 else MINUS if v < 0 else GRAY
        out.append({"type": "box", "layout": "horizontal", "spacing": "sm", "alignItems": "center", "contents": [
            _text(label, "xxs", "#555555", flex=label_flex),
            {"type": "box", "layout": "horizontal", "flex": 3, "height": "10px", "backgroundColor": "#EEEEEE",
             "cornerRadius": "3px", "contents": [{"type": "box", "layout": "vertical", "width": f"{width}%",
                                                   "backgroundColor": color, "cornerRadius": "3px", "contents": []}]},
            _text(vt, "xs", color, bold=True, align="end", flex=3)]})
    return out


def rule(strong: bool = False) -> dict:
    """横線（表の行の区切り）。strong は見出しの下の濃い線"""
    return {"type": "separator", "margin": "sm", "color": "#9CA3AF" if strong else RULE}


def note(text: str) -> dict:
    """④ 注記（小さい灰色の字）"""
    return _text(text, "xxs", GRAY, margin="md")


def yen_color(v: float) -> str:
    return PLUS if v > 0 else MINUS if v < 0 else TEXT


def bubble(head: dict, body: list[dict]) -> dict:
    return {"type": "bubble", "size": "giga", "header": head,
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body}}


def message(alt: str, bub: dict) -> dict:
    """Flex メッセージ（altText は通知一覧に出る1文・400字まで）"""
    return {"type": "flex", "altText": (alt or "通知")[:400], "contents": bub}


# ───── 文章をそのままカードに（個別の表を作っていない通知の共通の見た目・2026-10-19） ─────
_ACT_MARKS = ("⚠", "🔴", "❌", "✖", "NG", "失敗", "止ま", "異常")


def text_card(text: str, route: str = "INVEST", level: str | None = None, title: str | None = None) -> dict:
    """文章を読み取ってカードに並べる。1行目「【種類】残り」→ 見出しの帯、「■」→ 小見出し、「・」→ 横線つきの行、
    「※」→ 注記、「⚠🔴」で始まる行 → 赤い太字、空行 → 区切り。level を渡さなければ ⚠🔴・失敗などの語があれば要対応、
    無ければ報告だけ（送り手が判定を持っているなら必ず渡す）"""
    lines = [l.rstrip() for l in str(text).strip().splitlines()]
    first = lines[0] if lines else "通知"
    sub = ""
    import re
    m = re.search(r"[【\[]([^】\]]{1,30})[】\]]", first)        # 【種類】か [種類] が1行目のどこかにあれば見出しにする
    if m:
        title = title or m.group(1)
        sub = (first[:m.start()].strip(" ☀️🔔📣") + " " + first[m.end():].strip()).strip()
    else:
        title = title or first[:40]
        sub = first[40:] if len(first) > 40 else ""
    if level is None:
        level = "act" if any(m in text for m in _ACT_MARKS) else "info"
    body: list[dict] = []
    prev_row = False
    rest = lines[1:]
    for k, raw in enumerate(rest):
        l = raw.strip()
        if not l:
            prev_row = False
            continue
        nxt = rest[k + 1] if k + 1 < len(rest) else ""
        # 字下げしていない行の次に字下げした行が続く＝見出し（朝ブリーフの「📅 今日の予定」など）
        ind = lambda x: len(x) - len(x.lstrip(" 　")) if x.strip() else -1   # noqa: E731  行頭の字下げの量
        heading_like = (ind(raw) < ind(nxt) and not l.startswith(("・", "※", "⚠", "🔴", "-", "•")))
        if l.startswith("■") or l.startswith("◆") or l.startswith("▼") or heading_like:
            body += section(l.lstrip("■◆▼ ").strip())
            prev_row = False
        elif l.startswith("※"):
            body.append(note(l))
        elif l[:1] in ("⚠", "🔴"):
            body.append(_text(l, "sm", MINUS, bold=True, margin="md"))
            prev_row = False
        elif l[:1] in ("・", "-", "•") or raw.startswith((" ", "　")):
            if prev_row:
                body.append(rule())
            indent = raw.startswith(("　　", "    ")) or (raw.startswith(("　", "  ")) and l[:1] not in ("・", "-", "•"))
            body.append(_text(l, "xxs" if indent else "xs", "#555555" if indent else TEXT,
                              margin="none" if indent else None))
            prev_row = True
        else:
            body.append(_text(l, "sm", TEXT, bold=not body))      # 本文の最初の行は結論として太字
            prev_row = False
    if not body:
        body.append(_text(sub or first, "sm", TEXT))
    return message(first, bubble(header(title, route, level, sub), body))


MAX_CARD_BYTES = 20_000     # これより大きいカードは付けない（LINE のバブルの上限より十分小さく）
MAX_CARD_LINES = 60         # 文章が長すぎるときはカードを付けず文章だけ


def messages(text: str, route: str = "INVEST", level: str | None = None, limit: int = 4800) -> list[dict]:
    """push の messages に入れる「文章＋共通のカード」。カードが作れない・大きすぎるときは文章だけ。
    独自の送信関数を持つ送り手は、[{"type": "text", ...}] の代わりにこれを渡す（2026-10-19）"""
    msgs = [{"type": "text", "text": str(text)[:limit]}]
    try:
        if len(str(text).splitlines()) <= MAX_CARD_LINES:
            card = text_card(text, route, level)
            if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= MAX_CARD_BYTES:
                msgs.append(card)
    except Exception:  # noqa: BLE001  カードの失敗で通知を落とさない
        pass
    return msgs


# ───── 送る ─────
def push(token: str, user: str, text: str, flex: dict | None = None, timeout: float = 15) -> tuple[bool, str]:
    """文章（＋Flex）を1回の push で送る。Flex が形式で断られた（400）ときだけ文章だけで送り直す"""
    text_msg = {"type": "text", "text": text[:4800]}
    tries = [[text_msg, flex], [text_msg]] if flex else [[text_msg]]
    why = ""
    for msgs in tries:
        body = json.dumps({"to": user, "messages": msgs}).encode("utf-8")
        req = urllib.request.Request(LINE_PUSH_URL, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return (200 <= r.status < 300), f"HTTP {r.status}" + ("（文章＋カード）" if len(msgs) == 2 else "")
        except urllib.error.HTTPError as e:
            why = f"HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}"
            if e.code != 400 or len(msgs) == 1:
                return False, why
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
    return False, why


def save_preview(flex: dict, path: Path) -> None:
    """DRY_RUN 用：Flex の JSON を保存（LINE の Flex Message Simulator に貼れば見た目を確かめられる）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(flex.get("contents", flex), ensure_ascii=False, indent=1), encoding="utf-8")
