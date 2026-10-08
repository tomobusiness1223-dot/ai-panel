"""本番（LINE 方式）の出力 work/*.csv から、レポート試作の「会話の中身」「条件×出現率」「基準と参照元」画面用 JSON を作る。
出力先: ../debates/20261005-ai-panel-experiment/results-20261006/dashboard_data2.json"""
import csv, json, collections, pathlib, math
ROOT = pathlib.Path(__file__).resolve().parent.parent
W = ROOT / "work"
OUT = ROOT.parent / "debates/20261005-ai-panel-experiment/results-20261006/dashboard_data2.json"
CAT = {"shampoo": "シャンプー", "toner": "化粧水", "credit_card": "クレジットカード", "protein": "プロテイン", "earbuds": "ワイヤレスイヤホン", "vod": "動画配信サービス"}
COND = json.load(open(ROOT / "pipeline/conditions.json", encoding="utf-8"))

def rows(n):
    f = W / f"{n}.csv"
    return list(csv.DictReader(open(f, encoding="utf-8"))) if f.exists() else []

def wilson(k, n, z=1.96):
    if n == 0: return (0, 0)
    p = k / n; d = 1 + z * z / n; c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0, c - h), min(1, c + h))

def norm_condition(cat, key, value):
    """条件辞書に当てる。(項目, 値) を返す。当たらなければ ('その他：'+key, value)。"""
    d = COND.get(cat, {})
    for item, spec in d.items():
        if key == item or key in spec.get("aliases", []) or key.startswith(item):
            return item, value
    return "その他：" + key, value

# ---- 提出単位の情報 ----
subs = collections.defaultdict(lambda: {"conditions": [], "criteria": [], "products": [], "sources": collections.Counter(), "source_titles": {}, "turns": collections.Counter(), "n_turns": 0, "personalization": "", "brands": set()})
for r in rows("conditions"):
    k, v = norm_condition(r["category"], r["key"], r["value"]); subs[(r["person_id"], r["category"])]["conditions"].append([k, v])
for r in rows("criteria"): subs[(r["person_id"], r["category"])]["criteria"].append(r["criterion"])
for r in rows("products"):
    s = subs[(r["person_id"], r["category"])]; s["products"].append([int(r["rank"]), r["brand"], r["product"]]); s["brands"].add(r["brand"])
for r in rows("sources"):
    s = subs[(r["person_id"], r["category"])]
    if r["is_answer_turn"] == "1":
        s["sources"][r["domain"]] += 1; s["source_titles"].setdefault(r["domain"], r["title"][:60])
for r in rows("turns"):
    s = subs[(r["person_id"], r["category"])]; s["turns"][r["kind"]] += 1; s["n_turns"] += 1
for r in rows("responses"):
    subs[(r["person_id"], r["category"])]["personalization"] = r.get("personalization", "")

out = {"generated": "2026-10-08", "source": "LINE＋LIFF 方式の本番データ（運営者の試し提出）", "items": [], "by_category": {}}
for (pid, cat), s in sorted(subs.items(), key=lambda kv: list(CAT).index(kv[0][1]) if kv[0][1] in CAT else 99):
    if cat not in CAT: continue
    s["products"].sort()
    out["items"].append({"category": cat, "label": CAT.get(cat, cat), "conditions": s["conditions"], "criteria": s["criteria"], "products": s["products"],
                         "hearing_rounds": s["turns"].get("reply", 0), "interim": s["turns"].get("interim", 0), "n_turns": s["n_turns"],
                         "sources": [[d, n, s["source_titles"].get(d, "")] for d, n in s["sources"].most_common(8)], "n_sources": sum(s["sources"].values()),
                         "personalization": s["personalization"]})

# ---- カテゴリ単位の集計：条件×出現率、基準出現率、参照ドメインのシェア ----
for cat in CAT:
    people = {pid: s for (pid, c), s in subs.items() if c == cat}
    n = len(people)
    if not n: continue
    brands = collections.Counter(b for s in people.values() for b in s["brands"])
    top = [b for b, _ in brands.most_common(10)]
    # 条件 → その条件の人 → ブランド出現率
    cond_table = {}
    for pid, s in people.items():
        for k, v in s["conditions"]:
            cond_table.setdefault(k, {}).setdefault(v, set()).add(pid)
    cond_rows = []
    for k, vals in cond_table.items():
        for v, pids in vals.items():
            m = len(pids)
            cells = {}
            for b in top:
                kk = sum(1 for p in pids if b in people[p]["brands"]); lo, hi = wilson(kk, m)
                cells[b] = {"n": m, "k": kk, "rate": kk / m, "lo": lo, "hi": hi}
            cond_rows.append({"item": k, "value": v, "n": m, "cells": cells})
    crit = collections.Counter(c for s in people.values() for c in set(s["criteria"]))
    dom = collections.Counter(); dom_people = collections.defaultdict(set)
    for pid, s in people.items():
        for d in s["sources"]:
            dom_people[d].add(pid)
    dom_rows = [[d, len(p), len(p) / n] for d, p in sorted(dom_people.items(), key=lambda kv: -len(kv[1]))[:12]]
    out["by_category"][cat] = {"label": CAT[cat], "n": n, "top": top, "condition_rows": cond_rows, "criteria": [[c, k, k / n] for c, k in crit.most_common(12)], "domains": dom_rows,
                               "n_with_sources": sum(1 for s in people.values() if s["sources"])}
OUT.write_text(json.dumps(out, ensure_ascii=False))
print(f"{len(out['items'])} items, {len(out['by_category'])} categories -> {OUT}")
