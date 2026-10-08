"""本番（LINE 方式）の出力 work/*.csv から、レポート試作の「会話の中身」画面用 JSON を作る。
出力先: ../debates/20261005-ai-panel-experiment/results-20261006/dashboard_data2.json"""
import csv, json, collections, pathlib
W = pathlib.Path(__file__).resolve().parent.parent / "work"
OUT = pathlib.Path(__file__).resolve().parent.parent.parent / "debates/20261005-ai-panel-experiment/results-20261006/dashboard_data2.json"
CAT = {"shampoo": "シャンプー", "toner": "化粧水", "credit_card": "クレジットカード", "protein": "プロテイン", "earbuds": "ワイヤレスイヤホン", "vod": "動画配信サービス"}
def rows(n): return list(csv.DictReader(open(W / f"{n}.csv", encoding="utf-8")))
subs = collections.defaultdict(lambda: {"conditions": [], "criteria": [], "products": [], "sources": collections.Counter(), "source_titles": {}, "turns": collections.Counter(), "n_turns": 0, "hearing_rounds": 0, "personalization": ""})
for r in rows("conditions"): subs[(r["person_id"], r["category"])]["conditions"].append([r["key"], r["value"]])
for r in rows("criteria"): subs[(r["person_id"], r["category"])]["criteria"].append(r["criterion"])
for r in rows("products"): subs[(r["person_id"], r["category"])]["products"].append([int(r["rank"]), r["brand"], r["product"]])
for r in rows("sources"):
    s = subs[(r["person_id"], r["category"])]
    if r["is_answer_turn"] == "1":
        s["sources"][r["domain"]] += 1; s["source_titles"].setdefault(r["domain"], r["title"][:60])
for r in rows("turns"):
    s = subs[(r["person_id"], r["category"])]; s["turns"][r["kind"]] += 1; s["n_turns"] += 1
resp = {(r["person_id"], r["category"]): r for r in rows("responses")} if (W / "responses.csv").exists() else {}
out = {"generated": "2026-10-08", "source": "LINE＋LIFF 方式の本番データ（運営者の試し提出。各カテゴリ1人）", "items": []}
for (pid, cat), s in sorted(subs.items(), key=lambda kv: list(CAT).index(kv[0][1]) if kv[0][1] in CAT else 99):
    s["products"].sort()
    out["items"].append({"category": cat, "label": CAT.get(cat, cat), "conditions": s["conditions"], "criteria": s["criteria"], "products": s["products"],
                         "hearing_rounds": s["turns"].get("reply", 0), "interim": s["turns"].get("interim", 0), "n_turns": s["n_turns"],
                         "sources": [[d, n, s["source_titles"].get(d, "")] for d, n in s["sources"].most_common(8)], "n_sources": sum(s["sources"].values()),
                         "personalization": resp.get((pid, cat), {}).get("personalization", "")})
OUT.write_text(json.dumps(out, ensure_ascii=False))
print(f"{len(out['items'])} items -> {OUT}")
