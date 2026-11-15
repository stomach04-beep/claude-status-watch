# -*- coding: utf-8 -*-
"""
status_ja.py ― status.claude.com の英語（障害の件名・部品名・進み具合）を、AI を使わずに日本語へ置き換える。

【何をするか】（VBAでいうと「Replace と Like を並べた変換表」）
  - 件名は型が決まっている（"Elevated errors on X" "Degraded performance for X" など）ので、
    型ごとの正規表現で「何が（X）」「どうなった」を取り出し、日本語の文に組み直す。
  - X（対象）は「, 」「 and 」で区切り、1つずつ語句の対応表かモデル名・製品名の形で日本語にする。
  - 1語でも訳せない英語が残ったら、その件名は「訳せなかった」（None）として扱う。
    中途半端な英語まじりの文（半訳）は出さない＝呼び出し側が別の手で補う。
  - 製品名・モデル名（Claude Opus 5.5、Claude Code、claude.ai など）は英字のまま残す（固有名詞）。

【使う場所】claude_status_watch.py の通知文を組むときだけ。state.json と CSV は英語の原文のまま残す。
【自己テスト】python status_ja.py --selftest（過去の実件名を全部訳せるか・英語が残らないかを確かめる）
"""
from __future__ import annotations

import re

# ─────────────────────────────────────────────
# 固有名詞（英字のまま残してよい語）
#   ここに無い英単語が訳文に残ったら「訳せなかった」とみなす
# ─────────────────────────────────────────────
MODEL_RE = r"Claude (?:Fable|Opus|Sonnet|Haiku|Mythos)(?: \d+(?:\.\d+)?)?"     # Claude Opus 5.5 など。新しい版番号も形で通す
PROPER_WORDS = {
    "Claude", "Fable", "Opus", "Sonnet", "Haiku", "Mythos", "Code", "Cowork", "API", "Console",
    "Platform", "Developer", "Desktop", "iOS", "Android", "Windows", "macOS", "Mac", "Chrome",
    "Apple", "Google", "Microsoft", "Slack", "GitHub", "Gmail", "Drive", "Excel", "Word",
    "PowerPoint", "Outlook", "Teams", "Bedrock", "Vertex", "AWS", "MCP", "SSO", "Max", "Pro",
    "Team", "Enterprise", "Web", "Ultra", "Free",
}
# ドメイン名（小文字の英字だが固有名）
PROPER_DOMAINS = ("platform.claude.com", "api.anthropic.com", "console.anthropic.com",
                  "status.claude.com", "claude.ai", "claude.com")

# ─────────────────────────────────────────────
# 名詞の対応表（小文字で引く。先頭の the は外してから引く）
# ─────────────────────────────────────────────
NOUN_JA = {
    "multiple models": "複数のモデル",
    "requests to multiple models": "複数のモデルへの要求",
    "some models": "一部のモデル",
    "connectors": "コネクタ",
    "google connectors": "Google のコネクタ",
    "the microsoft 365 connector": "Microsoft 365 のコネクタ",
    "microsoft 365 connector": "Microsoft 365 のコネクタ",
    "credit purchases": "クレジットの購入",
    "credits": "クレジット",
    "purchases": "購入",
    "usage data": "使用量のデータ",
    "billing": "請求",
    "login": "ログイン",
    "logins": "ログイン",
    "sign up": "新規登録",
    "sign-ups": "新規登録",
    "signups": "新規登録",
    "file uploads": "ファイルのアップロード",
    "uploads": "アップロード",
    "artifacts": "アーティファクト",
    "projects": "プロジェクト",
    "web search": "Web 検索",
    "voice mode": "音声モード",
    "image generation": "画像生成",
    "the claude platform": "Claude Platform",
    "the claude api": "Claude API",
    "the claude ios app": "Claude の iOS アプリ",
    "the claude android app": "Claude の Android アプリ",
    "the claude desktop app": "Claude のデスクトップアプリ",
    "claude desktop": "Claude のデスクトップアプリ",
    "the api": "API",
    "message batches": "一括処理（Message Batches）",
    "the batch api": "一括処理（Batch API）",
    "prompt caching": "プロンプトのキャッシュ",
    "claude in chrome": "Claude in Chrome",
    "remote control": "リモート操作",
    "scheduled tasks": "定期実行タスク",
    "routines": "定期実行",
    "skills": "スキル",
    "memory": "メモリ",
    "research": "リサーチ",
}

# 形容詞・前置きの語（件名の頭の「エラー」「遅延」などの前に付く語）
ADJ_JA = {"purchasing": "購入の", "login": "ログインの", "authentication": "認証の", "api": "API の",
          "billing": "請求の", "upload": "アップロードの", "connector": "コネクタの"}

# 末尾の範囲を絞る語（落とすと実際より大げさに見えるので必ず訳して残す）
SCOPE_SUFFIX = [
    (re.compile(r"\s+for some users$", re.I), "（一部の利用者）"),
    (re.compile(r"\s+for some customers$", re.I), "（一部の利用者）"),
    (re.compile(r"\s+on Windows$", re.I), "（Windows 版）"),
    (re.compile(r"\s+on macOS$", re.I), "（macOS 版）"),
    (re.compile(r"\s+on (?:the )?Claude iOS app$", re.I), "（iOS アプリ）"),
    (re.compile(r"\s+on (?:the )?Claude Android app$", re.I), "（Android アプリ）"),
    (re.compile(r"\s+in some regions$", re.I), "（一部の地域）"),
]

NOUN_JA["claude services"] = "Claude の各サービス"

# 動詞（「〜できない」の型で使う）
VERB_JA = {"sign up": "新規登録", "sign in": "ログイン", "log in": "ログイン", "login": "ログイン",
           "access": "利用", "use": "利用", "purchase": "購入", "upload": "アップロード"}

# 件名の型（上から順に当てる。{x} {what} {where} は名詞句＝tr_np で訳す）
_X = r"(?P<x>.+)"
TITLE_PATTERNS = [
    (r"^Elevated errors loading (?P<what>.+?) on (?P<where>.+)$", "{where}で{what}の読み込みエラーが増加"),
    (r"^Elevated (?:rates of )?errors (?:on requests to|on|for|affecting|with|in) " + _X + "$", "{x}でエラーが増加"),
    (r"^Elevated (?P<adj>\w+) errors (?:on|for|affecting|with|in) " + _X + "$", "{x}で{adj}エラーが増加"),
    (r"^Elevated (?:latency|response times) (?:on|for|in|with) " + _X + "$", "{x}で応答の遅れ"),
    (r"^Increased (?:errors|error rates) (?:on|for|affecting|with|in) " + _X + "$", "{x}でエラーが増加"),
    (r"^Intermittent (?:error spikes|errors) (?:for|on|affecting|with|in) " + _X + "$", "{x}で断続的にエラーが急増"),
    (r"^Degraded performance (?:for|on|of|in) " + _X + "$", "{x}の性能低下"),
    (r"^Degraded functionality (?:for|on|of|in) " + _X + "$", "{x}の一部機能が不調"),
    (r"^Delays? in " + _X + "$", "{x}の遅れ"),
    (r"^Delayed (?P<what>.+?) on (?P<where>.+)$", "{where}で{what}の遅れ"),
    (r"^Delayed " + _X + "$", "{x}の遅れ"),
    (r"^(?:Partial |Major )?(?:Service )?(?:disruption|outage) (?:on|of|for|affecting|with|in) " + _X + "$", "{x}が停止"),
    (r"^Errors? (?:on|for|affecting|with|in) " + _X + "$", "{x}でエラー"),
    (r"^Issues? (?:on|for|affecting|with|in) " + _X + "$", "{x}で不具合"),
    (r"^" + _X + r" (?:is |are )?(?:unavailable|down)$", "{x}が使えない"),
]
TITLE_PATTERNS = [(re.compile(p, re.I), t) for p, t in TITLE_PATTERNS]
# 「(一部の利用者が) 〜できない」の型
UNABLE_RE = re.compile(r"^(?P<who>Some users )?(?:are )?unable to (?P<verb>sign up|sign in|log in|login|access|use|purchase|upload)"
                       r"(?: with (?P<via>\S+))?(?: (?:to |on |in )?(?P<x>.+))?$", re.I)


def is_proper(s: str) -> bool:
    """s が固有名詞（製品名・モデル名・ドメイン）と数字だけでできていれば True"""
    t = s
    for d in PROPER_DOMAINS:
        t = t.replace(d, " ")
    t = re.sub(MODEL_RE, " ", t)
    words = re.findall(r"[A-Za-z][A-Za-z0-9+.\-]*", t)
    return all(w.rstrip(".") in PROPER_WORDS for w in words)


def leftover_english(s: str) -> list[str]:
    """訳文に残った「固有名詞でない英単語」を返す（空なら日本語だけ＋固有名詞）"""
    t = s
    for d in PROPER_DOMAINS:
        t = t.replace(d, " ")
    t = re.sub(MODEL_RE, " ", t)
    return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9+\-]*", t) if w not in PROPER_WORDS]


def tr_np(s: str) -> str | None:
    """名詞句（"claude.ai, Claude Cowork and the Claude API" など）を日本語に。訳せない語があれば None"""
    out = []
    for p in re.split(r",\s*(?:and\s+)?|\s+and\s+", s.strip()):
        p = p.strip()
        if not p:
            continue
        key = p.lower()
        if key in NOUN_JA:
            out.append(NOUN_JA[key])
            continue
        bare = re.sub(r"^the\s+", "", p, flags=re.I)
        if bare.lower() in NOUN_JA:
            out.append(NOUN_JA[bare.lower()])
        elif is_proper(bare):
            out.append(bare)
        else:
            return None
    return "、".join(out) if out else None


def translate_title(name: str) -> str | None:
    """件名を日本語に。型に当てはまらない・英語が残るときは None（半訳は返さない）"""
    s = (name or "").strip().rstrip(".")
    if not s:
        return None
    notes = []
    changed = True
    while changed:                                # 「〜 on Windows for some users」のように重なっても全部拾う
        changed = False
        for rx, ja in SCOPE_SUFFIX:
            if rx.search(s):
                s = rx.sub("", s)
                notes.insert(0, ja)
                changed = True
    res = None
    m = UNABLE_RE.match(s)
    if m:
        verb = VERB_JA[m.group("verb").lower()]
        who = "一部の利用者が" if m.group("who") else ""
        via = f"{m.group('via')} で" if m.group("via") else ""
        x = ""
        if m.group("x"):
            xj = tr_np(m.group("x"))
            if xj is None:
                return None
            x = f"{xj}の"
        res = f"{who}{via}{x}{verb}ができない"
    else:
        for rx, tmpl in TITLE_PATTERNS:
            m = rx.match(s)
            if not m:
                continue
            parts = {}
            for k, v in m.groupdict().items():
                if k == "adj":
                    j = ADJ_JA.get(v.lower())
                else:
                    j = tr_np(v)
                if j is None:
                    parts = None
                    break
                parts[k] = j
            if parts is None:
                return None                       # 型は合ったが中身の語が分からない＝半訳を避けて諦める
            res = tmpl.format(**parts)
            break
    if res is None:
        return None
    res += "".join(notes)
    return None if leftover_english(res) else res


# ─────────────────────────────────────────────
# 部品名・進み具合
# ─────────────────────────────────────────────
COMPONENT_JA = {
    "claude.ai": "claude.ai（アプリ・Web 版）",
    "Claude API (api.anthropic.com)": "Claude API",
    "Claude Code": "Claude Code",
    "Claude Cowork": "Claude Cowork",
    "Claude Console (platform.claude.com)": "Claude Console（開発者向けの管理画面）",
    "Claude for Government": "Claude for Government（米政府向け）",
}
STATUS_JA = {
    "investigating": "調査中",
    "identified": "原因を特定・対応中",
    "monitoring": "直ったか様子見中",
    "resolved": "解決済み",
    "postmortem": "解決済み（振り返り公開）",
    "scheduled": "予定",
    "in_progress": "作業中",
    "verifying": "確認中",
    "completed": "完了",
}
COMPONENT_STATUS_JA = {
    "degraded_performance": "性能低下",
    "partial_outage": "一部停止",
    "major_outage": "停止",
    "under_maintenance": "メンテナンス中",
}


def component_ja(name: str) -> str:
    """部品名を日本語に。表に無い名前は固有名詞だけなら英字のまま、そうでなければ「その他の部品」"""
    if name in COMPONENT_JA:
        return COMPONENT_JA[name]
    for prefix in ("Claude ", "claude."):
        if name.startswith(prefix) and is_proper(re.sub(r"\(.*?\)", "", name)):
            return re.sub(r"\s*\(.*?\)", "", name)
    return "その他の部品"


def status_ja(status: str) -> str:
    return STATUS_JA.get(status, "状況は公式ページで")


# ─────────────────────────────────────────────
# 自己テスト（過去の実件名を全部訳せるか・英語が残らないか）
# ─────────────────────────────────────────────
SAMPLE_TITLES = [
    "Elevated errors for multiple models",
    "Some users unable to sign up with Apple on the Claude iOS app",
    "Intermittent error spikes for Claude Mythos 5.1 and Claude Fable 5.1",
    "Elevated latency on the Claude API for some users",
    "Elevated errors on requests to multiple models",
    "Elevated errors on platform.claude.com",
    "Elevated errors on Google connectors",
    "Elevated errors loading usage data on platform.claude.com",
    "Elevated errors affecting the Microsoft 365 connector",
    "Elevated errors affecting Connectors",
    "Delays in credit purchases",
    "Delayed credits on the Claude Platform",
    "Degraded performance for multiple models",
    "Degraded performance for Claude Opus 5 and Claude Haiku 4.5",
    "Degraded performance for Claude Fable 5",
    "Degraded functionality for Claude Cowork on Windows",
    "Elevated errors on claude.ai, Claude Cowork and the Claude API",
    "Elevated purchasing errors on claude.ai",
    "Service disruption on Claude services",
]
# 訳せてはいけない（半訳を出さず None を返すべき）件名
MUST_FAIL = [
    "Elevated errors when using extended thinking on long prompts",
    "Claude is returning truncated responses",
    "Degraded performance for tool use in some regions with streaming",
]


def _selftest() -> int:
    ng = 0
    for t in SAMPLE_TITLES:
        j = translate_title(t)
        print(("OK  " if j else "NG  ") + f"{t}\n    → {j}")
        ng += j is None
    for t in MUST_FAIL:
        j = translate_title(t)
        print(("OK  " if j is None else "NG  ") + f"(訳さない) {t} → {j}")
        ng += j is not None
    for c in ["claude.ai", "Claude API (api.anthropic.com)", "Claude Console (platform.claude.com)", "Claude Cowork",
              "Claude Desktop", "Some new thing"]:
        print(f"部品 {c} → {component_ja(c)}")
    print(f"結果: NG {ng} 件")
    return 1 if ng else 0


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
