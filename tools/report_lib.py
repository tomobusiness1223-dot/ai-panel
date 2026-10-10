"""カテゴリ別レポートの下ごしらえ（共通部品）。
本番から出力した work/*.csv（伏字済みの会話・読み取り結果・属性）を読み、会話を「最初の推薦／質問／答え／最終の推薦」に分ける。
ブランドと順位の読み取りは、本番と同じ pipeline.extract と seed.py の辞書を使う。"""
import csv, re, sys, pathlib, collections
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.extract import compile_brands, extract_brands, ranked_items, asks_questions, memory_sentences
import seed
W = ROOT / "work"
EXCLUDE = {"1"}   # 運営者の試し提出

def rows(name):
    f = W / f"{name}.csv"
    return [r for r in csv.DictReader(open(f, encoding="utf-8")) if r.get("person_id", "x") not in EXCLUDE] if f.exists() else []

def compiled_for(cat_key):
    makers = seed.MAKERS.get(cat_key, {})
    return compile_brands([(i, name, "\n".join(a for a in al.split("|") if a), makers.get(name)) for i, (name, al) in enumerate(seed.BRANDS[cat_key].items())])

def strip_chips(text):
    return text   # 提案チップは取得時に外すようになった（pipeline.share_html.CHIPS）。古い呼び出しのために残す

HEAD = re.compile(r"\n(?:#{1,4} |\-{3,}\s*\n|※|\|)")
def item_blocks(text, compiled):
    """順位つきで薦められた商品ごとに {rank, brand, product, text}。辞書に無い商品は brand=None。
    本文は、その項目の先頭から次の項目（または見出し・区切り・表）の手前まで。
    順位の一覧が無い回答（文章で1つだけ薦めるなど）は、薦められたブランドを出た順に並べる。"""
    out = []
    for it in ranked_items(text, compiled):
        body = text[it["start"]:it["end"]]
        m = HEAD.search(body, 5)
        if m:
            body = body[:m.start()]
        out.append({"rank": it["rank"], "brand": it["brand"], "product": it["product"], "text": body.strip(), "numbered": True})
    if not out:
        fs = sorted([f for f in extract_brands(text, compiled) if f.mention_type == "recommended"], key=lambda f: f.pos)
        for i, f in enumerate(fs, 1):
            s0 = text.rfind("\n", 0, f.pos) + 1
            e0 = text.find("\n\n", f.pos); e0 = len(text) if e0 < 0 else e0
            out.append({"rank": i, "brand": f.name, "product": f.product or f.name, "text": text[s0:e0].strip(), "numbered": False})
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
