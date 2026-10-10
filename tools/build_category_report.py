"""カテゴリ別レポート（見本）のデータを作る。
  python tools/build_category_report.py toner             → 本番から出力を取り直して work/report_toner.json を作る
  python tools/build_category_report.py toner --no-fetch  → work/ にある CSV で作る
  python tools/build_category_report.py toner --html <ひな形.html> <出力.html>  → データを埋め込んだ HTML も作る（ひな形の /*__DATA__*/ を置き換える）

会話を「前提 → 最初の推薦 → AI の質問 → 参加者の答え → 最終の推薦」に分け、1人1行の記録にする。集計は画面側で行う（ブランドを切り替えて見られるように）。
段階の決め方：
  updated    … 参加者が質問に答え、そのあとに推薦が出た（最初と最終の両方がある）
  first_only … 質問文を送って最初の推薦は出たが、答えたあとの推薦が無い（答える前に共有、または答えた直後に共有）
  single     … 回答1件だけの共有（chatgpt.com/s/）。会話のどの時点かが分からないので、段階の集計には入れない
"""
import sys, csv, json, re, collections, pathlib, subprocess, datetime
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import report_lib as L
import seed
from pipeline.extract import MEMORY_MARK, memory_sentences, STRONG
D = json.load(open(ROOT / "pipeline/report_dict.json", encoding="utf-8"))
W = ROOT / "work"
NAMES = ["participants", "responses", "turns", "own_brand", "premises", "sources", "own_answers", "conditions"]

def clip(s, n=44):
    s = re.sub(r"\s+", " ", s.replace("**", "")).strip(" 　*・-。")
    s = re.sub(r"^(?:おすすめの?理由|選定理由|理由|ポイント|特徴)\s*[:：]\s*", "", s)
    return s if len(s) <= n else s[:n - 1] + "…"

# ---- 商品1件の読み取り ----
NOTE = re.compile(r"^\s*(?:[-・*\s]*)(?:\*\*)?\s*(?:注意|注意点|※|ただし|デメリット|気になる点|購入時)")
PRICE = re.compile(r"[¥￥]\s?(\d{1,2},\d{3}|\d{3,5})|(?<![\d,])(\d{1,2},\d{3}|\d{3,4})\s*(?:[〜～~\-–][\d,]+\s*)?円")
WHO_LABEL = re.compile(r"(?:こんな人におすすめ|向いている人|おすすめの人|おすすめ)\s*[:：]\s*\**\s*(.+)")
def body_lines(text):
    ls = [l.strip() for l in text.split("\n") if l.strip()]
    return [l for l in ls[1:] if not NOTE.match(l)]
def tags_of(cat, text):
    body = "\n".join(body_lines(text))
    return [name for name, pat in D[cat]["reason_tags"] if re.search(pat, body)]
def price_of(text):
    for l in body_lines(text)[:8]:
        m = PRICE.search(l)
        if m:
            v = int((m.group(1) or m.group(2)).replace(",", ""))
            if 200 <= v <= 30000:
                return v
    return None
def who_of(text):
    ls = [l.strip(" *・-") for l in body_lines(text)]
    for l in ls[:6]:
        m = WHO_LABEL.search(l)
        if m:
            return clip(m.group(1))
    for l in ls[:3]:
        if 6 <= len(l) <= 40 and re.search(r"(人|方)(に|向け)|なら|重視|したい", l) and not re.search(r"\d\s*円|m[lL]|価格|参考|税込", l):
            return clip(l)
    for l in ls[:8]:
        m = re.search(r"([^。：:、]{4,36}?(?:人|方)(?:に|向け))", l)
        if m and not re.search(r"\d\s*円", m.group(1)):
            return clip(m.group(1))
    return ""
REASON = re.compile(r"(?:おすすめ(?:の|する)?理由|選定理由|選んだ理由|理由)\**\s*[:：]\**\s*(.+)")
def reason_of(text):
    """AI がその商品に付けた理由の、最初の一文（商品についての説明。本人の情報ではない）。"""
    ls = body_lines(text)
    for l in ls[:10]:
        m = REASON.search(l)
        if m and len(m.group(1).strip(" *")) >= 8:
            return clip(m.group(1).split("。")[0], 74)
    for l in ls[:8]:
        t = l.strip(" *・-")
        if len(t) >= 14 and not re.search(r"価格|\d\s*円|[¥￥]|m[lL]|商品情報|公式|購入先|販売", t):
            return clip(t.split("。")[0], 74)
    return ""
def line_of(cat, brand, product):
    for name, pat in D[cat]["lines"].get(brand or "", []):
        if re.search(pat, product or ""):
            return name
    return ""
def enrich(cat, items):
    out, seen = [], set()
    for it in items:
        if not it["brand"] and not it["product"]:
            continue
        ln = line_of(cat, it["brand"], it["product"])
        key = (it["rank"], it["brand"], ln or it["product"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"r": it["rank"], "b": it["brand"] or "", "l": ln, "p": clip(it["product"], 40), "y": price_of(it["text"]), "t": tags_of(cat, it["text"]), "w": who_of(it["text"]), "x": reason_of(it["text"])})
    return out

# ---- AI の質問 ----
def question_axes(cat, text):
    axes = []
    for line in text.split("\n"):
        l = line.strip()
        if not l or len(l) > 70:
            continue
        if re.match(r"^[-・]\s", l) and not re.search(r"[?？]\s*$", l):
            continue
        if not (l.startswith("■") or re.search(r"[?？]\s*(?:[（(][^）)]{0,14}[）)])?\s*[*＊）)]*$", l) or re.search(r"教えて(ください|ね|！|!)?\s*[*＊。]*$", l)):
            continue
        if re.search(r"どれを買うべき|買うのがいい|買うなら|おすすめは", l):
            continue
        for name, pat in D[cat]["question_axes"]:
            if re.search(pat, l):
                if name not in axes:
                    axes.append(name)
                break
    return axes

# ---- 前提（AI が知っていること） ----
NONE_PAT = re.compile(r"(保存され(?:ている|た)|記憶している|把握している|メモリ)[^。\n]{0,24}(?:ありません|ないです|無い|見当たりません|持っていません)|参考にできる[^。\n]{0,16}情報は(?:ほとんど|まだ)?(?:ありません|ない)")
def premise_info(text, comp):
    cats = [name for name, pat in D["premise_cats"] if re.search(pat, text)]
    brands = sorted({name for _, name, pat, _ in comp if pat.search(text)})
    return {"len": len(text), "none": bool(NONE_PAT.search(text[:500])) and len(text) < 700, "cats": cats, "brands": brands}

# ---- 参照元 ----
def source_type(domain):
    d = domain.lower().replace("www.", "")
    st = D["source_types"]
    if d in st:
        return st[d]
    for k, v in st.items():
        if d.endswith("." + k):
            return v
    return ["その他のサイト", d]

PAST = re.compile(r"(以前|前回|先日|これまで|最近|過去に)[^。\n?？]{0,40}(伺|お話|話し|聞い|教えてもらっ|教えていただ|相談され|相談した|探して|気になって|使って|言って|おっしゃ|ようだ|ようです|でしたね|ましたね|ありました)")
AGE_ORDER = ["10代", "20代", "30代", "40代", "50代", "60代", "70代"]
def build(cat):
    comp = L.compiled_for(cat)
    C = L.conversations(cat)
    parts = {r["person_id"]: r for r in L.rows("participants")}
    conds = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in L.rows("conditions"):
        if r["category"] == cat and r["value"] not in conds[r["person_id"]][r["key"]]:
            conds[r["person_id"]][r["key"]].append(r["value"])
    own = {r["person_id"]: r for r in L.rows("own_brand") if r["category"] == cat}
    prem = {}
    for r in L.rows("premises"):
        if r.get("accept_status") in ("accepted", "reference") and r["text"].strip():
            prem[r["person_id"]] = r["text"]
    src = collections.defaultdict(dict)
    for r in L.rows("sources"):
        if r["category"] == cat:
            d = r["domain"].lower().replace("www.", "")
            src[r["person_id"]][d] = src[r["person_id"]].get(d, 0) + 1
    people = []
    for pid, c in sorted(C.items(), key=lambda x: int(x[0])):
        a = parts.get(pid, {})
        users = [t for t in c["turns"] if t["role"] == "user"]
        stage = "updated" if c["updated"] else ("single" if not users else "first_only")
        first, final = enrich(cat, c["first"]), enrich(cat, c["final"])
        ai_first_text = c["first_text"] or ""
        mem_s = memory_sentences(ai_first_text)
        # 引用するのは「以前〜と伺いました」「最近〜気になっていたようだから」のように、過去の会話に触れた文だけ（文の頭から句点まで）。印だけの回答は引用しない
        txt = ai_first_text.replace(MEMORY_MARK, "")
        m = PAST.search(txt)
        if m:
            s0 = max(txt.rfind("\n", 0, m.start()), txt.rfind("。", 0, m.start())) + 1
            e0 = txt.find("。", m.end()); e0 = len(txt) if e0 < 0 else e0 + 1
            mem_ev = txt[s0:e0].replace("\n", " ")
        else:
            mem_ev = ""
        ob = own.get(pid)
        p = {"pid": pid, "who": f"{a.get('age_decade') or '年代不明'}{ {'F': '女性', 'M': '男性'}.get(a.get('gender'), '') }",
             "g": a.get("gender", ""), "age": a.get("age_decade", ""), "memory": a.get("memory", ""), "freq": a.get("usage_freq", ""), "shop": a.get("shopping_ai_freq", ""),
             "stage": stage, "rounds": len(c["replies"]), "asked": question_axes(cat, c["ask_text"]) if c["asked"] else [],
             "conds": {k: v for k, v in conds.get(pid, {}).items()},
             "first": first if stage != "single" else [], "final": final if stage == "updated" else [], "single": first if stage == "single" else [],
             "own": {"b": (ob or {}).get("brand", ""), "raw": clip((ob or {}).get("raw_text", ""), 30)} if ob and (ob.get("raw_text") or "").strip() not in ("", "なし", "無し", "ない", "特になし") else None,
             "prem": premise_info(prem[pid], comp) if pid in prem else None,
             "mem": {"marked": bool(mem_s), "ev": clip(mem_ev.replace(MEMORY_MARK, ""), 110)} if (mem_ev or mem_s) else None,
             "src": sorted(([d, source_type(d)[0], n] for d, n in src.get(pid, {}).items()), key=lambda x: -x[2])}
        people.append(p)
    stages = collections.Counter(p["stage"] for p in people)
    lead, item, unit = seed.LEAD[cat]
    meta = {"category": cat, "label": next((n for k, n, *_ in seed.CATEGORIES if k == cat), cat), "prompt": seed.PROMPT.format(lead=lead, item=item, unit=unit), "n": len(people), "n_first": stages["updated"] + stages["first_only"], "n_updated": stages["updated"], "n_first_only": stages["first_only"], "n_single": stages["single"],
            "n_cond": sum(1 for p in people if p["conds"]), "n_asked": sum(1 for p in people if p["asked"]), "n_prem": sum(1 for p in people if p["prem"]),
            "n_src": sum(1 for p in people if p["src"]), "generated": datetime.date.today().isoformat()}
    labels = {d: v[1] for d, v in D["source_types"].items()}
    cases_f = W / f"cases_{cat}.json"
    return {"meta": meta, "people": people, "tags": [t[0] for t in D[cat]["reason_tags"]], "axes": [t[0] for t in D[cat]["question_axes"]],
            "premise_cats": [t[0] for t in D["premise_cats"]], "source_labels": labels, "maker_domains": D["maker_domains"],
            "makers": {n: m for _, n, _, m in comp if m}, "cases": json.load(open(cases_f, encoding="utf-8")) if cases_f.exists() else []}

def summary(d):
    P = d["people"]; m = d["meta"]
    print(f"人数 {m['n']}（最初の推薦あり {m['n_first']}、答えたあとの推薦あり {m['n_updated']}、単独回答 {m['n_single']}）、条件あり {m['n_cond']}、前提あり {m['n_prem']}、参照元あり {m['n_src']}")
    def brands(key, ps):
        c = collections.Counter(); r1 = collections.Counter()
        for p in ps:
            for b in {i["b"] for i in p[key] if i["b"]}: c[b] += 1
            top = [i for i in p[key] if i["r"] == 1 and i["b"]]
            if top: r1[top[0]["b"]] += 1
        return c, r1
    F = [p for p in P if p["first"]]; U = [p for p in P if p["stage"] == "updated"]
    cf, r1f = brands("first", F); cl, r1l = brands("final", U); cfu, _ = brands("first", U)
    print("最初の推薦（n=%d）:" % len(F), ", ".join(f"{b}{n}" for b, n in cf.most_common(10)), "| 1位:", ", ".join(f"{b}{n}" for b, n in r1f.most_common(6)))
    print("最終の推薦（n=%d）:" % len(U), ", ".join(f"{b}{n}" for b, n in cl.most_common(10)), "| 1位:", ", ".join(f"{b}{n}" for b, n in r1l.most_common(6)))
    ch = sum(1 for p in U if (p["first"] and p["final"] and p["first"][0]["b"] != p["final"][0]["b"]))
    ch_l = sum(1 for p in U if (p["first"] and p["final"] and (p["first"][0]["b"], p["first"][0]["l"]) != (p["final"][0]["b"], p["final"][0]["l"])))
    print(f"1位のブランドが入れ替わった: {ch}/{len(U)}、1位の商品（ライン）が入れ替わった: {ch_l}/{len(U)}")
    ax = collections.Counter(a for p in P for a in p["asked"])
    print("AI の質問:", ", ".join(f"{a}{n}" for a, n in ax.most_common()), f"（n={m['n_asked']}）")
    tg = collections.Counter(t for p in P for k in ("first", "final", "single") for i in p[k] for t in i["t"])
    print("理由タグ:", ", ".join(f"{a}{n}" for a, n in tg.most_common()))
    nw = sum(1 for p in P for k in ("first", "final", "single") for i in p[k]); ww = sum(1 for p in P for k in ("first", "final", "single") for i in p[k] if i["w"]); yy = sum(1 for p in P for k in ("first", "final", "single") for i in p[k] if i["y"])
    ln = sum(1 for p in P for k in ("first", "final", "single") for i in p[k] if i["l"]); nb = sum(1 for p in P for k in ("first", "final", "single") for i in p[k] if not i["b"])
    print(f"商品 {nw} 件：『誰向け』あり {ww}、価格あり {yy}、ライン判定あり {ln}、辞書外 {nb}")
    pc = collections.Counter(c for p in P if p["prem"] for c in p["prem"]["cats"])
    print("前提の分類:", ", ".join(f"{a}{n}" for a, n in pc.most_common()), "| 何も無い:", sum(1 for p in P if p["prem"] and p["prem"]["none"]), "| ブランドあり:", [(p["pid"], p["prem"]["brands"]) for p in P if p["prem"] and p["prem"]["brands"]])
    print("メモリ・過去の会話に触れた最初の回答:", [(p["pid"], p["mem"]["marked"], p["mem"]["ev"][:50]) for p in P if p["mem"]])
    st = collections.Counter(t for p in P for t in {s[1] for s in p["src"]})
    print("参照元の種類（人数）:", dict(st))
    ow = [(p["pid"], p["own"]["b"] or p["own"]["raw"]) for p in P if p["own"]]
    print("現使用ブランド:", ow)

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cat = args[0] if args else "toner"
    if "--no-fetch" not in sys.argv:
        for n in NAMES:
            subprocess.run([sys.executable, str(ROOT / "tools/annotate.py"), "export", n, "wide"], check=True, capture_output=True)
    d = build(cat)
    out = W / f"report_{cat}.json"
    out.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{out}: {out.stat().st_size // 1024} KB")
    summary(d)
    if "--html" in sys.argv:
        i = sys.argv.index("--html"); tpl, dst = sys.argv[i + 1], sys.argv[i + 2]
        html = open(tpl, encoding="utf-8").read()
        assert "/*__DATA__*/" in html, "ひな形に /*__DATA__*/ がありません"
        page = html.replace("/*__DATA__*/", json.dumps(d, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
        open(dst, "w", encoding="utf-8").write(page)     # 公開用（Artifact）。<html> などの外枠は付けない
        local = pathlib.Path(dst).with_name(pathlib.Path(dst).stem + "_local.html")
        local.write_text('<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><style>body{margin:0}</style></head><body>' + page + "</body></html>", encoding="utf-8")
        print("HTML:", dst, "／ 手元で開く用:", local)

if __name__ == "__main__":
    main()
