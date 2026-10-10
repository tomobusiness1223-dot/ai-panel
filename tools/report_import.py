"""取り込んだ回答の集計（2回目の収集の途中確認用）。
  python tools/report_import.py            → 本番から出力を取り直して集計
  python tools/report_import.py --no-fetch → work/ にある CSV で集計
除外: person_id 1（運営者の試し提出）。"""
import sys, csv, collections, pathlib, subprocess
ROOT = pathlib.Path(__file__).resolve().parent.parent
W = ROOT / "work"
EXCLUDE = {"1"}
NAMES = ["participants", "responses", "mentions", "products", "first_products", "turns", "own_brand", "premises", "sources"]
if "--no-fetch" not in sys.argv:
    for n in NAMES:
        subprocess.run([sys.executable, str(ROOT / "tools/annotate.py"), "export", n, "wide"], check=True, capture_output=True)
def rows(n):
    f = W / f"{n}.csv"
    return [r for r in csv.DictReader(open(f, encoding="utf-8")) if r.get("person_id", "x") not in EXCLUDE] if f.exists() else []
CAT = {"toner": "化粧水", "credit_card": "クレジットカード", "protein": "プロテイン", "premise": "前提"}
resp = rows("responses"); turns = rows("turns"); prods = rows("products"); first = rows("first_products"); own = rows("own_brand"); prem = rows("premises")
parts = rows("participants")
print(f"参加者（取り込み分）: {len(parts)} 人\n")
# ---- お題ごとの状態 ----
print("■ お題ごとの受付状態（responses があるもの＝受付／参考）")
by = collections.defaultdict(collections.Counter)
for r in resp:
    by[r["category"]][r["match_status"]] += 1
for c in ("premise", "toner", "credit_card", "protein"):
    if by[c]:
        print(f"  {CAT[c]:10s} 受付 {sum(by[c].values()):3d} 件:", ", ".join(f"{k} {v}" for k, v in by[c].most_common()))
# ---- 2段階の流れ ----
print("\n■ 2段階の流れ（カテゴリお題）")
kinds = collections.defaultdict(set)
for t in turns:
    kinds[(t["person_id"], t["category"])].add(t["kind"])
for c in ("toner", "credit_card", "protein"):
    keys = [k for k in kinds if k[1] == c]
    n = len(keys)
    two = sum(1 for k in keys if "interim" in kinds[k] and "answer" in kinds[k] and "reply" in kinds[k])
    noq = sum(1 for k in keys if "reply" not in kinds[k])
    early = sum(1 for r in resp if r["category"] == c and r["match_status"] == "early")
    print(f"  {CAT[c]:10s} n={n:3d}  最初の推薦→質問→答え→最終推薦: {two:3d} ({two/n:.0%})  往復なし（質問されなかった/答えなかった）: {noq:3d}  答える前に共有(early): {early:3d}")
# ---- 読み取り ----
print("\n■ 読み取れた推薦（最終）")
pp = collections.defaultdict(list)
for p in prods:
    pp[(p["person_id"], p["category"])].append(p)
for c in ("toner", "credit_card", "protein"):
    ks = [k for k in pp if k[1] == c]
    cnt = [len(pp[k]) for k in ks]
    zero = sum(1 for r in resp if r["category"] == c and r["extract_status"] == "none")
    top = collections.Counter(p["brand"] for k in ks for p in pp[k]).most_common(8)
    firsts = collections.Counter(p["brand"] for k in ks for p in pp[k] if p["rank"] == "1").most_common(5)
    print(f"  {CAT[c]:10s} 推薦あり {len(ks)} 件、平均 {sum(cnt)/max(1,len(cnt)):.1f} 個、読み取れず {zero} 件")
    print(f"     出現: " + ", ".join(f"{b}{n}" for b, n in top))
    print(f"     1位: " + ", ".join(f"{b}{n}" for b, n in firsts))
# ---- 最初と最終の違い ----
# 分母は「参加者が質問に答え、そのあとに推薦が出た会話」だけ（report_lib の updated）。答える前の共有、答えた直後の共有、回答1件だけの共有は入れない
print("\n■ 最初の推薦（質問される前）と最終の推薦の違い")
sys.path.insert(0, str(ROOT / "tools"))
import report_lib as RL
for c in ("toner", "credit_card", "protein"):
    U = [x for x in RL.conversations(c).values() if x["updated"] and x["first"] and x["final"]]
    if not U: continue
    top = lambda xs: sorted(xs, key=lambda i: i["rank"])[0]["brand"]
    bs = lambda xs: {i["brand"] for i in xs if i["brand"]}
    changed1 = sum(1 for x in U if top(x["first"]) != top(x["final"]))
    gone = [len(bs(x["first"]) - bs(x["final"])) / max(1, len(bs(x["first"]))) for x in U]
    print(f"  {CAT[c]:10s} 答えたあとの推薦まである会話 {len(U)} 件、1位が入れ替わった {changed1} 件 ({changed1/len(U):.0%})、最初の推薦のうち最終で消えた割合 平均 {sum(gone)/len(gone):.0%}")
# ---- 現使用ブランド ----
print("\n■ いま使っているブランド")
for c in ("toner", "credit_card", "protein"):
    os_ = [o for o in own if o["category"] == c]
    named = [o for o in os_ if o["raw_text"] and o["raw_text"] not in ("なし", "無し", "ない")]
    mapped = [o for o in named if o["brand"]]
    hit = sum(1 for o in mapped if o["brand"] in {p["brand"] for p in pp.get((o["person_id"], c), [])})
    unm = collections.Counter(o["raw_text"] for o in named if not o["brand"])
    print(f"  {CAT[c]:10s} 回答 {len(os_)}、ブランドあり {len(named)}、辞書で正規化 {len(mapped)}、現使用ブランドが推薦に出た {hit}/{len(mapped)}")
    if unm: print(f"     辞書に無い: " + ", ".join(f"{k}({v})" for k, v in unm.most_common(10)))
# ---- 前提 ----
print("\n■ 前提のお題")
pr = [p for p in prem if p["person_id"] not in EXCLUDE]
acc = [p for p in pr if p["accept_status"] in ("accepted", "reference")]
print(f"  受付 {len(acc)} / 提出 {len(pr)}。平均 {sum(len(p['text']) for p in acc)/max(1,len(acc)):.0f} 字")
# ---- 個人化 ----
print("\n■ 個人化の手がかり（規則版）")
pz = collections.Counter(r["personalization"] for r in resp if r["category"] != "premise")
print("  ", dict(pz))
