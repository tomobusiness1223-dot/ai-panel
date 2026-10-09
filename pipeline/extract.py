"""回答の構造化（仕様書3.2）：質問一致の判定、推薦ブランドの抽出と順位、言及の種類、個人化の手がかり。"""
from __future__ import annotations
import re, unicodedata, difflib, dataclasses
from .fetch import Message

FALLBACK_PROMPT = "おまかせします。おすすめを5つ教えてください。"
FALLBACK_PROMPTS = (FALLBACK_PROMPT, "おまかせします。おすすめを教えてください。")
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
        if norm(second_text) in {norm(x) for x in FALLBACK_PROMPTS}:
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
    product: str = ""        # 回答に書かれていた商品名（例：「Aujua（オージュア） スムース シャンプー」）
    primary: bool = True     # そのブランドの代表の行（同じブランドが複数の順位に出たら、最上位だけ True）

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

EXTRACT_VERSION = "v7"   # 抽出ルールを変えたら上げる。古い版で作った提出は backfill が作り直す
BOLD = re.compile(r"\*\*(.+?)\*\*")
RANK_ONLY = re.compile(r"^\s*(?:第)?\d{1,2}\s*位?\s*$|^[①-⑩]$")

def _product_text(text: str, start: int, end: int, block: tuple[int, int] | None) -> str:
    """ブランドが出た位置から、回答に書かれた商品名を取り出す。
    1) その出現を含む太字 2) 同じ項目の最初の太字（順位だけの太字は除く） 3) 表のセル 4) その行"""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end); le = len(text) if le < 0 else le
    line = text[ls:le]
    for m in BOLD.finditer(line):
        if ls + m.start() <= start and end <= ls + m.end():
            return _tidy(m.group(1))
    if re.match(r"\s*(#{1,6}\s|(?:[-・*\s]*)(?:\d{1,2}[.．)）、:：]|[①-⑩]|(?:第)?\d{1,2}\s*位))", line) and "|" not in line:
        return _tidy(line)          # 見出し・番号つきの行に書かれている場合は、その行が商品名
    if block and "|" not in line:
        for m in BOLD.finditer(text[block[0]:block[1]]):
            seg = m.group(1)
            if not RANK_ONLY.match(seg) and len(seg) <= 80 and block[0] + m.start() <= start + 200:
                return _tidy(seg)
    if "|" in line:
        pos = 0
        for cell in line.split("|"):
            if pos <= start - ls <= pos + len(cell):
                return _tidy(cell)
            pos += len(cell) + 1
    return _tidy(line)

def _tidy(s: str) -> str:
    s = re.sub(r"[*#`]", "", s)
    s = re.sub(r"^\s*(?:[-・\s|]*)(?:\d{1,2}[.．)）、:：]|[①-⑩]|(?:第)?\d{1,2}\s*位)[\s｜|:：・·\-–—は]*", "", s)
    return re.sub(r"\s+", " ", s).strip()[:80]

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
            head.setdefault(int(mi.group(1)), o)   # 項目として書かれた順位があれば、そちらを優先
    # 4) 順位つきの項目は1項目＝1行（同じブランドが別商品で複数の順位に出ることがある）
    block_span = {n: (s, e_) for n, s, e_ in blocks}
    found: list[Found] = []
    seen = set()
    head_names_by_block = {n: o[2] for n, o in head.items()}
    ranked_names = set()
    for n, o in sorted(head.items()):
        name = o[2]
        found.append(Found(ids[name], name, o[0], n, True, "recommended",
                           product=_product_text(text, o[0], o[1], block_span.get(n)), primary=name not in ranked_names))
        ranked_names.add(name); seen.add(name)
    for o in occ:
        name = o[2]
        if name in seen:
            continue
        seen.add(name)
        mtype = "negative" if is_negative(o) else "recommended"
        if mtype == "recommended":
            # 同じ項目の先頭ブランドのメーカーなら「メーカー名の表記」として数えない
            h = head_names_by_block.get(block_of(o[0]))
            if h and makers.get(h) == name:
                mtype = "compared"
            elif any(makers.get(r) == name for r in ranked_names) and not blocks:
                mtype = "compared"
        found.append(Found(ids[name], name, o[0], None, False, mtype, product=_product_text(text, o[0], o[1], None)))
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

def pick_dialog_answer(messages: list[Message], compiled) -> tuple[str, int, str]:
    """ヒアリングありの会話で、分析に使う回答＝会話の結論を選ぶ。
    推薦ブランドを1つ以上含む assistant 回答のうち、最後のもの（途中の一覧は conversation_turn に残る）。"""
    best, n_lists = None, 0
    for i, m in enumerate(messages):
        if m.role != "assistant":
            continue
        n = sum(1 for f in extract_brands(m.text, compiled) if f.mention_type == "recommended" and f.primary)
        if n >= 1:
            best = (m.text, i, n); n_lists += 1
    if best is None:
        last = [(m.text, i) for i, m in enumerate(messages) if m.role == "assistant"]
        return (last[-1][0], last[-1][1], "推薦を含む回答なし。最後の回答を使用") if last else ("", -1, "回答なし")
    note = f"最後の推薦つき回答を使用（推薦{best[2]}件）"
    if n_lists > 1:
        note += f"。途中にも推薦つきの回答が{n_lists - 1}件あり"
    return (best[0], best[1], note)

ASKS = re.compile(r"[?？]|教えてください|お聞かせ|お知らせください|選んでください|どれに近い|ありますか|ですか")

def stage_info(messages: list[Message], compiled) -> dict:
    """2段階の質問文（先に薦めてから質問）の会話を調べる。
    first＝推薦を含む最初の回答、final＝推薦を含む最後の回答。early＝AI が質問しているのに参加者が答える前に共有された。"""
    recs = []
    for i, m in enumerate(messages):
        if m.role == "assistant" and any(f.mention_type == "recommended" for f in extract_brands(m.text, compiled)):
            recs.append(i)
    users = [i for i, m in enumerate(messages) if m.role == "user"]
    assistants = [i for i, m in enumerate(messages) if m.role == "assistant"]
    replied = len(users) >= 2
    last_ai = messages[assistants[-1]].text if assistants else ""
    tail = last_ai[-400:]
    asks = len(ASKS.findall(tail)) >= 1
    return {"first": recs[0] if recs else None, "final": recs[-1] if recs else None, "n_stages": len(recs),
            "early": bool(assistants) and not replied and asks}

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


# ---- ヒアリングの答えから条件を取り出す（選択式フォームの答えは「項目：値。」の並びになる） ----
COND = re.compile(r"(?:^|[。\n])\s*([^。：:\n]{1,20})[：:]\s*([^。\n]{1,80})(?=。|\n|$)")
def conditions_from_reply(text: str) -> list[tuple[str, str]]:
    out = []
    for m in COND.finditer(text):
        k, v = m.group(1).strip(), m.group(2).strip()
        if k and v and not re.search(r"https?$", k):
            out.append((k, v))
    return out
