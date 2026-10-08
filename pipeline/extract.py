"""回答の構造化（仕様書3.2）：質問一致の判定、推薦ブランドの抽出と順位、言及の種類、個人化の手がかり。"""
from __future__ import annotations
import re, unicodedata, difflib, dataclasses
from .fetch import Message

FALLBACK_PROMPT = "おまかせします。おすすめを5つ教えてください。"
LENGTH_HINT = re.compile(r"(\d{2,4}\s*(字|文字)|簡潔|短く|箇条書き|まとめて|要約)")

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\s　]+", "", s)
    s = re.sub(r"[、，,。．.!！?？「」『』\"'“”‘’・…]", "", s)
    return s.strip()

@dataclasses.dataclass
class MatchResult:
    status: str                 # ok / typo / turn / followup / fallback / dialog / modified / samechat / fail
    answer: str                 # 分析に使う assistant の回答本文
    answer_index: int           # messages 内の位置
    note: str = ""

def match_prompt(prompt: str, messages: list[Message], mode: str = "single") -> MatchResult:
    """mode="dialog" のお題では、質問文のあとに AI が質問し参加者が答える往復を認め、最後の回答を使う。"""
    users = [(i, m) for i, m in enumerate(messages) if m.role == "user"]
    assistants = [(i, m) for i, m in enumerate(messages) if m.role == "assistant"]
    if not assistants:
        return MatchResult("fail", "", -1, "assistant の回答がない")
    if not users:  # s/t_ 形式：1回答だけの共有
        i, m = assistants[0]
        return MatchResult("turn", m.text, i, "質問文は確認できない")
    target = norm(prompt)
    def kind(text: str) -> str:
        t = norm(text)
        if t == target:
            return "ok"
        if target in t and LENGTH_HINT.search(text):
            return "modified"
        r = difflib.SequenceMatcher(None, t, target).ratio()
        if r >= 0.9 and not LENGTH_HINT.search(text):
            return "typo"
        if r >= 0.8 or target in t:
            return "modified"
        return "other"
    kinds = [(i, kind(m.text)) for i, m in users]
    first_i, first_k = kinds[0]
    def answer_after(idx: int):
        for j, m in assistants:
            if j > idx:
                return j, m.text
        return -1, ""
    if first_k in ("ok", "typo"):
        j, ans = answer_after(first_i)
        if j < 0:
            return MatchResult("fail", "", -1, "質問の後に回答がない")
        if len(users) == 1:
            return MatchResult(first_k, ans, j)
        # 2回目以降のユーザー発言
        second_text = users[1][1].text
        if norm(second_text) == norm(FALLBACK_PROMPT):
            j2, ans2 = answer_after(users[1][0])
            # 1回目が質問返しだった場合は2回目の回答を使う（定型文ルール）
            return MatchResult("fallback", ans2 or ans, j2 if ans2 else j, "定型文を1回送った")
        if mode == "dialog":
            # ヒアリングの往復を経た最後の回答を使う
            j_last, m_last = assistants[-1]
            return MatchResult("dialog", m_last.text, j_last, f"往復あり（ユーザー発言{len(users)}回）。最後の回答を使用")
        return MatchResult("followup", ans, j, "回答のあとに追加の指示あり。1回目の回答を使用")
    if first_k == "modified":
        return MatchResult("modified", "", -1, "質問文に付け足し・変更あり")
    # 1問目が別の質問 → 指定質問が後にあるか
    for i, k in kinds[1:]:
        if k in ("ok", "typo"):
            j, ans = answer_after(i)
            return MatchResult("samechat", ans, j, "同じチャットで前に別の質問あり")
    return MatchResult("fail", "", -1, "指定の質問文が見つからない")

# ---- ブランド抽出 ----
NUM_LINE = re.compile(r"^\s*(?:[#*\-\s|]*)(?:(\d{1,2})[.．)）、:：]|[①②③④⑤⑥⑦⑧⑨⑩]|(?:第)?(\d{1,2})\s*位)", re.M)
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
INLINE_RANK = re.compile(r"(\d{1,2})\s*位\s*[はがに：:｜|・\-–—]?\s*[*＊「『【\s]*$")
NEG_BEFORE = re.compile(r"(おすすめしない|おすすめできない|勧めない|勧められない|避け|向かない|除外|選ばない|やめ)")
NEG_AFTER = re.compile(r"(は(おすすめ|お勧め|勧め)(しない|できない|しません|できません|られない|られません|ません|ない)|は避け|は向か|は除外|は選ば|ではなく|以外)")

@dataclasses.dataclass
class Found:
    brand_id: int
    name: str
    pos: int
    rank: int | None
    is_numbered: bool
    mention_type: str

def compile_brands(brands: list[tuple]) -> list[tuple]:
    """brands: (id, canonical_name, aliases改行区切り[, maker])。canonical も別名に含める。戻り値は (id, name, pattern, maker)。"""
    out = []
    B = r"(?<![A-Za-z0-9\u30a1-\u30fa\u30fc-\u30ff])(?:{})(?![A-Za-z0-9\u30a1-\u30fa\u30fc-\u30ff])"  # 英数字・カタカナの途中では一致させない（リラックス≠ラックス）。中黒「・」は区切りとして扱う
    for row in brands:
        bid, name, aliases = row[0], row[1], row[2]
        maker = row[3] if len(row) > 3 else None
        pats = [a.strip() for a in (aliases or "").split("\n") if a.strip()]
        pats.append(re.escape(name))
        out.append((bid, name, re.compile(B.format("|".join(f"(?:{p})" for p in pats)), re.I), maker))
    return out

def _numbered_blocks(text: str) -> list[tuple[int, int, int]]:
    """(番号, 開始位置, 終了位置)。番号付き項目の範囲。"""
    marks = []
    for m in NUM_LINE.finditer(text):
        g = m.group(1) or m.group(2)
        if g:
            n = int(g)
        else:
            ch = m.group(0).strip()[0]
            n = CIRCLED.index(ch) + 1 if ch in CIRCLED else 0
        if 1 <= n <= 15:
            marks.append((n, m.start()))
    blocks = []
    for k, (n, s) in enumerate(marks):
        e = marks[k + 1][1] if k + 1 < len(marks) else len(text)
        blocks.append((n, s, e))
    # 1から始まる最初の連番だけを採る（「理由」の中の番号などを避ける）
    seq, expect = [], 1
    for n, s, e in blocks:
        if n == expect:
            seq.append((n, s, e)); expect += 1
    return seq

def extract_brands(text: str, compiled, mention_classifier=None) -> list[Found]:
    """本文からブランドを拾い、順位と言及の種類を付ける。
    順位は「番号つき項目（1. / ① / 1位）」ごとに、その項目で最初に出たブランドに付ける。"""
    blocks = _numbered_blocks(text)
    def block_of(p):
        for n, s, e_ in blocks:
            if s <= p < e_:
                return n
        return None
    makers = {name: maker for _, name, _, maker in compiled}
    ids = {name: bid for bid, name, _, _ in compiled}
    # 1) 全ブランドの全出現
    occ = []  # (start, end, name)
    for bid, name, pat, maker in compiled:
        for m in pat.finditer(text):
            occ.append((m.start(), m.end(), name))
    # 2) 別ブランドのより長い一致に含まれる出現は捨てる（「セゾン」⊂「セゾンプラチナ」など）
    occ = [o for o in occ if not any(p is not o and p[2] != o[2] and p[0] <= o[0] and o[1] <= p[1] and (p[1] - p[0]) > (o[1] - o[0]) for p in occ)]
    occ.sort(key=lambda o: (o[0], -(o[1] - o[0])))
    def is_negative(o):
        s0 = max(text.rfind("。", 0, o[0]), text.rfind("\n", 0, o[0])) + 1
        return bool(NEG_BEFORE.search(text[s0:o[0]]) or NEG_AFTER.match(text[o[1]:o[1] + 12]))
    # 3) 項目ごとの先頭ブランド
    head: dict[int, tuple] = {}
    for o in occ:
        if is_negative(o):
            continue
        n = block_of(o[0])
        if n is not None and n not in head:
            head[n] = o
    # 先頭がメーカー名で、同じ項目にそのメーカーのブランドが続くなら、ブランドを先頭にする（「花王 キュレル」）
    for n, h in list(head.items()):
        for o in occ:
            if block_of(o[0]) == n and makers.get(o[2]) == h[2] and not is_negative(o):
                head[n] = o; break
    # 文中の「4位は**ミノン**、5位は**イハダ**」
    for o in occ:
        mi = INLINE_RANK.search(text[max(0, o[0] - 14):o[0]])
        if mi and not is_negative(o):
            head[int(mi.group(1))] = o
    # 4) ブランドごとに1件にまとめる
    found: list[Found] = []
    ranked: dict[str, tuple[int, tuple]] = {}
    for n, o in sorted(head.items()):
        ranked.setdefault(o[2], (n, o))
    head_names_by_block = {n: o[2] for n, o in head.items()}
    seen = set()
    for name, (n, o) in ranked.items():
        found.append(Found(ids[name], name, o[0], n, True, "recommended")); seen.add(name)
    for o in occ:
        name = o[2]
        if name in seen:
            continue
        seen.add(name)
        mtype = "negative" if is_negative(o) else "recommended"
        if mtype == "recommended":
            # どの出現でも、同じ項目の先頭ブランドのメーカーなら「メーカー名の表記」として数えない
            n = block_of(o[0])
            h = head_names_by_block.get(n)
            if h and makers.get(h) == name:
                mtype = "compared"
            elif any(makers.get(r) == name for r in ranked) and not blocks:
                mtype = "compared"
        found.append(Found(ids[name], name, o[0], None, False, mtype))
    if mention_classifier:
        found = mention_classifier(text, found)
    # 5) 番号なしの推薦は、番号つきの後ろに初出順で並べる
    rec = [f for f in found if f.mention_type == "recommended"]
    for f in found:
        if f.mention_type != "recommended":
            f.rank = None
    next_rank = max((f.rank for f in rec if f.is_numbered and f.rank), default=0) + 1
    for f in sorted([f for f in rec if not f.is_numbered], key=lambda f: f.pos):
        f.rank = next_rank; next_rank += 1
    return found

def pick_dialog_answer(messages: list[Message], compiled, min_brands: int = 4) -> tuple[str, int, str]:
    """ヒアリングありの会話で、分析に使う回答を選ぶ。
    おすすめの一覧（推薦ブランドが min_brands 以上）を含む assistant 回答のうち最後のもの。
    一覧のあとに深掘りの質問が続いた会話（配分の試算など）で、最後の回答が一覧でなくなる場合に備える。"""
    best, best_n, last = None, -1, None
    for i, m in enumerate(messages):
        if m.role != "assistant":
            continue
        n = sum(1 for f in extract_brands(m.text, compiled) if f.mention_type == "recommended")
        last = (m.text, i)
        if n >= min_brands:
            best, best_n = (m.text, i), n
        elif best is None or (best_n < min_brands and n > best_n):
            best, best_n = (m.text, i), n
    if best is None:
        return ("", -1, "回答なし")
    note = "一覧を含む最後の回答を使用" if best_n >= min_brands else f"一覧が見つからず、推薦が最も多い回答を使用（{best_n}件）"
    if last and best[1] != last[1]:
        note += "（そのあとに追加のやり取りあり）"
    return (best[0], best[1], note)

# ---- 個人化の手がかり（規則版。LLM版は classifier_version を変えて置き換える） ----
STRONG = re.compile(r"(以前|前回|先日|前に|これまで)(の|に)?(ご)?(お話|話し|伺|おっしゃ|聞い|教えて|相談|購入|お使い|会話|やり取り)|(と|を)伺っています|ご利用中の|現在(お使い|ご利用|保有)|お使いの|お持ちの|ご登録|プロフィール|[一-鿿]{1,4}(さん|様)(の|、|は)|妊娠|お子さ|お子様|ご家族|ご主人|奥様|iPhone\s?\d{1,2}|Pixel\s?\d|Galaxy\s?S\d")
WEAK = re.compile(r"(あなたの|ご希望|お好み|向け|好きな|ライフスタイル|年代|世代|男性|女性)")

def personalization_level(text: str) -> tuple[str, str]:
    m = STRONG.search(text)
    if m:
        s = max(0, m.start() - 30); return "strong", text[s:m.end() + 30].replace("\n", " ")
    m = WEAK.search(text)
    if m:
        s = max(0, m.start() - 20); return "weak", text[s:m.end() + 20].replace("\n", " ")
    return "none", ""

def own_brand_lookup(raw: str, compiled) -> int | None:
    s = re.split(r"[、,/／・\s]", raw.strip())[0] if raw else ""
    if not s or re.fullmatch(r"(なし|無し|不明|特になし|使っていない|ない)", s):
        return None
    for bid, name, pat, _maker in compiled:
        if pat.search(s):
            return bid
    return None
