"""カテゴリ別レポートの下ごしらえ（共通部品）。
本番から出力した work/*.csv（伏字済みの会話・読み取り結果・属性）を読み、会話を「最初の推薦／質問／答え／最終の推薦」に分ける。
ブランドと順位の読み取りは、本番と同じ pipeline.extract と seed.py の辞書を使う。"""
import csv, re, sys, pathlib, collections
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.extract import compile_brands, extract_brands, _numbered_blocks, asks_questions
import seed
W = ROOT / "work"
EXCLUDE = {"1"}   # 運営者の試し提出

def rows(name):
    f = W / f"{name}.csv"
    return [r for r in csv.DictReader(open(f, encoding="utf-8")) if r.get("person_id", "x") not in EXCLUDE] if f.exists() else []

def compiled_for(cat_key):
    makers = seed.MAKERS.get(cat_key, {})
    return compile_brands([(i, name, "\n".join(a for a in al.split("|") if a), makers.get(name)) for i, (name, al) in enumerate(seed.BRANDS[cat_key].items())])

CHIPS = re.compile(r"\n-{3,}\s*\n+\s*必要であれば、次のことができます[\s\S]*$")   # ChatGPT の提案チップ（本文ではない）
def strip_chips(text):
    return CHIPS.sub("", text)

HEAD = re.compile(r"\n(?:#{1,4} |\-{3,}\s*\n|※|\|)")
def item_blocks(text, compiled):
    """順位つきで薦められた商品ごとに {rank, brand, product, text}。同じブランドの別商品も1行ずつ残す。
    本文は、その商品の行から次の商品の行（または見出し・区切り・表）の手前まで。"""
    text = strip_chips(text)
    found = sorted([f for f in extract_brands(text, compiled) if f.mention_type == "recommended" and f.rank], key=lambda f: f.pos)
    seen, items = set(), []
    for f in found:                      # 同じ順位に同じブランドが重ねて出たら最初の1つ
        if (f.rank, f.name) in seen:
            continue
        seen.add((f.rank, f.name)); items.append(f)
    out = []
    for i, f in enumerate(items):
        s = text.rfind("\n", 0, f.pos) + 1
        e = text.rfind("\n", 0, items[i + 1].pos) + 1 if i + 1 < len(items) else len(text)
        body = text[s:e] if e > s else text[s:]
        m = HEAD.search(body, 5)
        if m:
            body = body[:m.start()]
        out.append({"rank": f.rank, "brand": f.name, "product": f.product or f.name, "text": body.strip()})
    return out

def conversations(cat_key):
    """{person_id: {...}}。stage: first＝推薦を含む最初の AI 発言、final＝結論の発言。updated＝参加者の答えのあとに推薦が出たか。"""
    comp = compiled_for(cat_key)
    resp = {r["person_id"]: r for r in rows("responses") if r["category"] == cat_key}
    tt = collections.defaultdict(list)
    for t in rows("turns"):
        if t["category"] == cat_key:
            tt[t["person_id"]].append(t)
    out = {}
    for pid, r in resp.items():
        ts = sorted(tt.get(pid, []), key=lambda x: int(x["idx"]))
        ais = [t for t in ts if t["role"] == "assistant"]
        users = [t for t in ts if t["role"] == "user"]
        replies = [t for t in users if t["kind"] == "reply"]
        with_recs = [(t, item_blocks(t["text"], comp)) for t in ais]
        with_recs = [(t, b) for t, b in with_recs if b]
        first = with_recs[0] if with_recs else None
        ans = next(((t, b) for t, b in with_recs if t["kind"] == "answer"), with_recs[-1] if with_recs else None)
        first_reply_idx = int(replies[0]["idx"]) if replies else None
        updated = bool(ans and first_reply_idx is not None and int(ans[0]["idx"]) > first_reply_idx)
        asked = [t for t in ais if asks_questions(strip_chips(t["text"]))]
        out[pid] = {"pid": pid, "submission_id": r.get("submission_id"), "match": r["match_status"], "turns": ts, "replies": replies,
                    "first": first[1] if first else [], "first_text": strip_chips(first[0]["text"]) if first else "",
                    "final": ans[1] if ans else [], "final_text": strip_chips(ans[0]["text"]) if ans else "",
                    "updated": updated, "asked": bool(asked), "ask_text": strip_chips(asked[0]["text"]) if asked else "",
                    "n_sources_final": int(ans[0]["n_sources"] or 0) if ans else 0, "personalization": r.get("personalization", "")}
    return out
