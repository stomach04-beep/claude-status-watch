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

送り方：カードだけを送る（2026-10-19 ユーザー「通知はカードだけでいい」）。カードの形式で断られた（HTTP 400）ときだけ文章で送り直す。
通知一覧・トーク一覧に出る altText には文章の要点（1行目＋次の行）を入れる。

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

# ダークモード（2026-10-19 ユーザー「背景は暗め・白い字のほうが見やすい」）。帯はアカウントの色を少し落ち着かせ、本文は濃い灰色に白い字
ROUTE_COLOR = {"LIFE": "#166534", "INVEST": "#1D4ED8", "IPO": "#6D28D9", "SYSTEM": "#374151"}
BG = "#1F2937"        # 本文の背景（濃い灰。2026-10-19 案C：LINE の暗い背景から少し浮かせる）
FRAME = "#9CA3AF"     # カードを囲む灰色の枠（案C）
SUB = "#D1D5DB"       # 一段控えめな白（明細の字・横棒の名前）
TRACK = "#4B5563"     # 横棒の下地
LEVELS = {"act": "🔴 要対応", "look": "🟡 見ておく", "info": "🟢 報告だけ"}
PLUS, MINUS, GRAY, TEXT = "#4ADE80", "#F87171", "#9CA3AF", "#F9FAFB"   # 暗い背景で見える明るい緑・赤・灰・白
# 字の大きさは全部1段大きくして出す（2026-10-19 ユーザー「フォントを小さくしている部分が少し読みにくい」）。
# 部品の中は従来の段（xxs〜lg）で書き、ここで1段上げる＝1か所で全カードの字の大きさを変えられる
SIZE_UP = {"xxs": "xs", "xs": "sm", "sm": "md", "md": "lg", "lg": "xl", "xl": "xxl"}
RULE = "#4B5563"      # 表の横線の色（2026-10-19 ユーザー「横線があったほうが見やすい」・本文の灰より一段明るい）


# ───── 部品 ─────
def _text(text: str, size: str = "xs", color: str = TEXT, bold: bool = False, align: str | None = None,
          flex: int | None = None, margin: str | None = None) -> dict:
    t = {"type": "text", "text": str(text) if str(text) != "" else " ", "size": SIZE_UP.get(size, size), "color": color,
         "wrap": True}
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
    """① 見出しの帯（アカウントの色の帯に白い字）。sub は日付など。
    札（🔴要対応など）は中身の幅だけ取り、残りを全部題に使う（2026-10-22 題が狭くて3行に折り返していた）"""
    lv = _text(LEVELS.get(level, level), "xs", "#FFFFFF", align="end", flex=0)
    lv["wrap"] = False                                         # 札は1行のまま
    contents = [
        {"type": "box", "layout": "horizontal", "spacing": "md", "contents": [
            _text(title, "md", "#FFFFFF", bold=True, flex=1), lv]},
    ]
    if sub:
        contents.append(_text(sub, "xs", "#E5E7EB"))
    return {"type": "box", "layout": "vertical", "backgroundColor": ROUTE_COLOR.get(route, ROUTE_COLOR["INVEST"]),
            "paddingAll": "12px", "spacing": "xs", "contents": contents}


def kpi(text: str, color: str = TEXT) -> dict:
    """② 結論1つ（大きい字）"""
    return _text(text, "lg", color, bold=True)


def kpi_parts(parts: list[tuple[str, str | None]]) -> dict:
    """② 結論1つを、言葉は白・数字だけ色で（2026-10-19 ユーザー「きょうの合計の文字は赤以外に。下がったら赤・上がったら緑」）。
    parts は [(文字, 色 or None), ...]。None は白。1行の中で色を変えるので LINE の span を使う"""
    spans = [{"type": "span", "text": t, "color": c or TEXT, "weight": "bold"} for t, c in parts if t]
    return {"type": "text", "text": "".join(t for t, _c in parts) or " ", "size": SIZE_UP["lg"], "wrap": True,
            "weight": "bold", "color": TEXT, "contents": spans}


def money(label: str, v: float, vt: str, tail: str = "") -> list[tuple[str, str | None]]:
    """「きょうの合計 −52.8万円（9銘柄）」の形：言葉は白、金額だけプラス緑・マイナス赤・0は白"""
    return [(label, None), (vt, yen_color(v) if v else None), (tail, None)]


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
            _text(label, "xxs", SUB, flex=label_flex),
            {"type": "box", "layout": "horizontal", "flex": 3, "height": "10px", "backgroundColor": TRACK,
             "cornerRadius": "3px", "contents": [{"type": "box", "layout": "vertical", "width": f"{width}%",
                                                   "backgroundColor": color, "cornerRadius": "3px", "contents": []}]},
            _text(vt, "xs", color, bold=True, align="end", flex=3)]})
    return out


def rule(strong: bool = False) -> dict:
    """横線（表の行の区切り）。strong は見出しの下の濃い線"""
    return {"type": "separator", "margin": "sm", "color": "#6B7280" if strong else RULE}


def note(text: str) -> dict:
    """④ 注記（小さい灰色の字）"""
    return _text(text, "xxs", GRAY, margin="md")


def yen_color(v: float) -> str:
    """色の使い分け（全カード共通）：上がった・プラス＝緑、下がった・マイナス＝赤、増減なし＝白"""
    return PLUS if v > 0 else MINUS if v < 0 else TEXT


def bubble(head: dict, body: list[dict]) -> dict:
    """カード1枚（2026-10-19 案C：色の帯と本文を1つの箱に入れ、灰色の枠で囲む。LINE のカードは外側に直接枠を付けられないため）。
    箱の外（カード自体の地）は透明にして、LINE の暗い背景の上に枠つきの箱だけが見えるようにする"""
    head = dict(head, cornerRadius="9px")
    inner = {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px", "contents": body}
    frame = {"type": "box", "layout": "vertical", "borderWidth": "2px", "borderColor": FRAME, "cornerRadius": "12px",
             "backgroundColor": BG, "contents": [head, inner]}
    return {"type": "bubble", "size": "giga",
            "body": {"type": "box", "layout": "vertical", "paddingAll": "0px", "backgroundColor": "#00000000",
                     "contents": [frame]},
            "styles": {"body": {"backgroundColor": "#00000000"}}}


def alt_of(text: str) -> str:
    """通知一覧に出る1文：文章の1行目と、次の中身のある1行（カードだけ送るので要点が見えるように）"""
    ls = [l.strip() for l in str(text).splitlines() if l.strip()]
    return "　".join(ls[:2])[:400] if ls else "通知"


def message(alt: str, bub: dict) -> dict:
    """Flex メッセージ（altText は通知一覧に出る1文・400字まで）"""
    return {"type": "flex", "altText": (alt or "通知")[:400], "contents": bub}


# ───── 文章をそのままカードに（個別の表を作っていない通知の共通の見た目・2026-10-19） ─────
_ACT_MARKS = ("⚠", "🔴", "❌", "✖", "NG", "失敗", "止ま", "異常")

# 字下げの決まり（2026-10-22 ユーザー「左インデントを調整するなど全部見直して」→ Fable 相談）：
# 字の左端を 16px × 段 の列にそろえ、・や→ は左の 16px の溝に置く（折り返した2行目は記号の右から始まる＝ぶら下げ）。
# 段は box の paddingStart で付ける（offsetStart ははみ出す・全角スペースは字幅が一定でないので使わない）
STEP = 16                                   # 1段の幅（px）
_BULLETS = ("・", "-", "•")                 # 「・」として溝に置く記号
_ARROWS = ("→", "⇒", "└")                  # 子の行の記号（灰色で溝に置き、本文は一段控えめの白）


def _indent(raw: str) -> int:
    """行頭の字下げの量（半角1・全角2）。空行は -1"""
    if not raw.strip():
        return -1
    head = raw[:len(raw) - len(raw.lstrip(" 　"))]
    return sum(2 if c == "　" else 1 for c in head)


def _depth(width: int) -> int:
    """字下げの量 → 段（0＝字下げなし・1＝半角1〜2・2＝3〜4・3＝それ以上）"""
    return 0 if width <= 0 else 1 if width <= 2 else 2 if width <= 4 else 3


def _row(text: str, depth: int, mark: str = "", color: str = TEXT, size: str = "xs", mark_color: str = TEXT) -> dict:
    """段つきの1行。mark があれば幅16pxの溝に置き、本文は記号の右で折り返す（ぶら下げ）。
    mark が無ければ字の左端を段の列にそろえるだけ"""
    pad = STEP * (max(depth, 1) - 1) if mark else STEP * depth
    body = _text(text, size, color, flex=1)
    contents = ([{"type": "box", "layout": "vertical", "width": f"{STEP}px", "flex": 0, "contents": [_text(mark, size, mark_color)]}, body]
                if mark else [body])
    box = {"type": "box", "layout": "horizontal", "contents": contents}
    if pad:
        box["paddingStart"] = f"{pad}px"
    return box


def _split_title(first: str) -> tuple[str, str]:
    """1行目 → (見出しの帯の題, 副題)。先頭の絵文字・記号は落とす（札が絵文字の役目を持つ）。
    【種類】か [種類] があればそれが題。無ければ「（」「：」までを題に、無ければ最初の空白まで（24字まで）"""
    import re
    m = re.search(r"[【\[]([^】\]]{1,30})[】\]]", first)
    if m:
        rest = (first[:m.start()] + " " + first[m.end():])
        return m.group(1), re.sub(r"^[^\w\[【（(]+", "", rest.strip()).strip()
    s = re.sub(r"^[^\w\[【（(]+", "", first.strip())
    for seps in (("（", "(", "：", ":"), (" ", "　")):
        # 日付の曜日「10/22(木)」・時刻「13:05」の括弧やコロンでは切らない（直前が数字なら区切りとみなさない）
        cut = min((i for i, c in enumerate(s[:25]) if c in seps and i > 0 and not s[i - 1].isdigit()), default=-1)
        if 0 < cut <= 24:
            return s[:cut].strip(), s[cut:].strip(" 　：:")
    return (s[:24], s[24:]) if len(s) > 24 else (s, "")


def text_card(text: str, route: str = "INVEST", level: str | None = None, title: str | None = None) -> dict:
    """文章を読み取ってカードに並べる。1行目「【種類】残り」→ 見出しの帯、「■」→ 小見出し、「・」→ 横線つきの行、
    「※」→ 注記、「⚠🔴」で始まる行 → 赤い太字、空行 → 区切り。level を渡さなければ ⚠🔴・失敗などの語があれば要対応、
    無ければ報告だけ（送り手が判定を持っているなら必ず渡す）"""
    import re
    lines = [l.rstrip() for l in str(text).strip().splitlines()]
    first = lines[0] if lines else "通知"
    t0, sub = _split_title(first)
    title = title or t0 or first[:24]
    if level is None:
        level = "act" if any(m in text for m in _ACT_MARKS) else "info"
    body: list[dict] = []
    rest = lines[1:]
    head_depth = -1        # いまの見出しの段（その下の行は少なくとも1段深くする）
    seen: set[int] = set()  # いまの見出しの下で、行を置いたことのある段（同じ段どうしの間にだけ横線を入れる）
    parent = 0             # 直前の（→でない）行の段。→の子の行はこれより1段深くする
    for k, raw in enumerate(rest):
        l = raw.strip()
        if not l:
            continue
        w = _indent(raw)
        nxt_w = next((_indent(x) for x in rest[k + 1:] if x.strip()), -1)   # 次の中身のある行の字下げ
        d = _depth(w)
        # ── 1行ずつの種類の判定（ここ1か所だけで決める） ──
        if l.startswith(("■", "◆", "▼")) or (w < nxt_w and not l.startswith(_BULLETS + _ARROWS + ("※",))):
            # 見出し：■◆▼ か「次の行のほうが深く字下げ」（朝ブリーフの「📅 今日の予定」「⚠️ 期限切れ」など。先頭の絵文字は問わない）
            name = l.lstrip("■◆▼ ").strip()
            zero = re.search(r"(^|[^\d])0件", name) is not None                 # 0件の見出しは灰色で軽く
            if d == 0:
                if body:
                    body.append({"type": "separator", "margin": "lg"})
                hc = MINUS if l[:1] in ("⚠", "🔴") else GRAY if zero else TEXT          # ⚠🔴の見出しは警告の赤のまま
                body.append(_text(name, "sm", hc, bold=not zero, margin="md"))
            else:
                h = _row(name, d, color=GRAY if zero else SUB)
                h["contents"][-1]["weight"] = "bold"
                h["margin"] = "md"
                body.append(h)
            head_depth, seen = d, set()
        elif l.startswith("※"):
            body.append(dict(note(l), margin="xs"))                           # 注記は直前の行にくっつける
        elif w == 0 and l[:1] in ("⚠", "🔴"):
            body.append(_text(l, "sm", MINUS, bold=True, margin="md"))          # 警告の赤太字は字下げなしの行だけ
            seen = set()
        elif w > 0 or l.startswith(_BULLETS + _ARROWS):
            rd = max(d, head_depth + 1, 1)
            if l.startswith(_ARROWS):
                rd = max(rd, parent + 1)                                     # →は直前の親の行より1段深く
                r = _row(l[1:].strip(), rd, mark=l[0], color=SUB, size="xs", mark_color=GRAY)
            elif l.startswith(_BULLETS):
                r = _row(l[1:].strip(), rd, mark="・")
            else:
                r = _row(l, rd, color=TEXT if not re.match(r"[\w（(]", l) else SUB)   # 絵文字で始まる行は本文の白、文章は控えめの白
            if rd in seen and not l.startswith(_ARROWS):
                body.append(rule())                                           # 同じ段の行どうしの間だけ横線
            seen = {s for s in seen if s <= rd} | {rd}                       # 深い段の記録は親が変わったら忘れる
            if not l.startswith(_ARROWS):
                parent = rd
            body.append(r)
        else:
            body.append(_text(l, "sm", TEXT, bold=not body, margin=None if not body else "md"))   # 本文の最初の行は結論として太字
            head_depth, seen = -1, set()
    if not body:
        body.append(_text(sub or first, "sm", TEXT))
    return message(alt_of(text), bubble(header(title, route, level, sub), body))


MAX_CARD_BYTES = 20_000     # これより大きいカードは付けない（LINE のバブルの上限より十分小さく）
MAX_CARD_LINES = 60         # 文章が長すぎるときはカードを付けず文章だけ


def messages(text: str, route: str = "INVEST", level: str | None = None, limit: int = 4800) -> list[dict]:
    """push の messages に入れる「文章＋共通のカード」。カードが作れない・大きすぎるときは文章だけ。
    独自の送信関数を持つ送り手は、[{"type": "text", ...}] の代わりにこれを渡す（2026-10-19）"""
    text_msg = {"type": "text", "text": str(text)[:limit]}
    try:
        if len(str(text).splitlines()) <= MAX_CARD_LINES:
            card = text_card(text, route, level)
            if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= MAX_CARD_BYTES:
                return [card]                      # カードだけ（2026-10-19）
    except Exception:  # noqa: BLE001  カードの失敗で通知を落とさない
        pass
    return [text_msg]


# ───── 送る ─────
def push(token: str, user: str, text: str, flex: dict | None = None, timeout: float = 15) -> tuple[bool, str]:
    """文章（＋Flex）を1回の push で送る。Flex が形式で断られた（400）ときだけ文章だけで送り直す"""
    text_msg = {"type": "text", "text": text[:4800]}
    tries = [[flex], [text_msg]] if flex else [[text_msg]]     # カードだけ。断られたら文章だけで送り直す
    why = ""
    for msgs in tries:
        body = json.dumps({"to": user, "messages": msgs}).encode("utf-8")
        req = urllib.request.Request(LINE_PUSH_URL, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return (200 <= r.status < 300), f"HTTP {r.status}" + ("（カード）" if msgs[0].get("type") == "flex" else "（文章）")
        except urllib.error.HTTPError as e:
            why = f"HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}"
            if e.code != 400 or msgs[0].get("type") != "flex":
                return False, why
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
    return False, why


def save_preview(flex: dict, path: Path) -> None:
    """DRY_RUN 用：Flex の JSON を保存（LINE の Flex Message Simulator に貼れば見た目を確かめられる）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(flex.get("contents", flex), ensure_ascii=False, indent=1), encoding="utf-8")
