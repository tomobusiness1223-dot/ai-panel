"""クラウドワークスの作業結果 CSV を、本番の仕組み（取得→抽出→保存）に取り込む。

  python tools/import_crowdworks.py A path/to/タスクA.csv      # お題型（前提＋3カテゴリ）
  python tools/import_crowdworks.py B path/to/タスクB.csv      # 実会話型
  python tools/import_crowdworks.py status                      # 取り込み結果（承認判断用）を work/cw_status.csv に出す
  オプション --dry : 送らずに、読み取った内容だけ表示

CSV の列は、設問文に含まれる目印の語で探す（設問番号がずれても動く）。選択式の設問は「番号, 選択肢名」の2列になるので、選択肢名の列を読む。
"""
import sys, csv, json, pathlib, datetime, httpx
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from annotate import token, BASE, WORK
GENRES = json.load(open(ROOT / "pipeline/genres.json", encoding="utf-8"))
GENRE_BY_NAME = {it["name"]: it["key"] for g in GENRES["groups"] for it in g["items"]}

def col(header, row, *keys):
    """設問文に keys をすべて含む列の値。選択式なら次の列（選択肢名）を返す。"""
    for i, h in enumerate(header):
        if h and all(k in h for k in keys):
            if i + 1 < len(header) and header[i + 1] == "":
                return (row[i + 1] or "").strip()
            return (row[i] or "").strip()
    return ""

def multi(header, row, key):
    """複数選択（N-1, N-2 … の列に分かれる）の選択肢名の一覧。"""
    out = []
    for i, h in enumerate(header):
        if h and key in h and i + 1 < len(header) and header[i + 1] == "" and (row[i + 1] or "").strip():
            out.append(row[i + 1].strip())
    return out

YEAR = datetime.date.today().year
AGE = {"18〜24歳": 21, "25〜29歳": 27, "30〜34歳": 32, "35〜39歳": 37, "40〜44歳": 42, "45〜49歳": 47, "50〜54歳": 52, "55〜59歳": 57, "60〜64歳": 62, "65歳以上": 68}
M = {
    "gender": {"女性": "female", "男性": "male"},
    "household": {"一人暮らし": "single", "夫婦・パートナーと": "couple", "親と": "with_parents", "子どもと": "family_kids", "その他": "other"},
    "ai_plan": {"無料": "free", "有料（Plus・Proなど）": "paid", "分からない": "unknown"},
    "memory_setting": {"ONにしている": "on", "OFFにしている": "off", "分からない": "unknown"},
    "usage_freq": {"ほぼ毎日": "daily", "週に数回": "weekly_several", "週1回くらい": "weekly", "月に数回以下": "monthly"},
    "started_at": {"3か月未満": "lt3m", "3か月〜1年": "3m_1y", "1年以上": "gt1y"},
    "shopping_ai_freq": {"よくある": "often", "たまにある": "sometimes", "ほとんどない": "never"},
    "device": {"iPhone": "iphone", "Androidのスマホ": "android", "パソコン": "pc"},
    "intent": {"ある": "yes", "ない": "no", "分からない": "unknown"},
    "will_refer": {"する": "yes", "しない": "no", "分からない": "unknown"},
    "outcome": {"買った・契約した": "bought", "検討中": "considering", "買わなかった": "not_bought"},
}
APPEAL = {"価格・料金が手頃だった": "price", "成分・性能・機能・内容が良さそうだった": "spec", "口コミ・評価が良かった": "review", "知っているブランド・会社だった": "brand", "買いやすかった・申し込みやすかった": "access", "自分の条件に合っていた": "fit", "その他": "other"}
REJECT = {"高かった": "price", "情報が足りなかった": "info", "信用できなかった": "trust", "店や人に別のものを勧められた": "store", "まだ迷っている": "undecided", "必要なくなった": "noneed", "その他": "other"}

def attributes(h, r):
    age = col(h, r, "年代")
    return {"gender": M["gender"].get(col(h, r, "性別"), "other"), "birth_year": (YEAR - AGE[age]) if age in AGE else None,
            "prefecture": col(h, r, "お住まい") or None, "household": M["household"].get(col(h, r, "同居")),
            "ai_plan": M["ai_plan"].get(col(h, r, "プラン")), "memory_setting": M["memory_setting"].get(col(h, r, "メモリ")),
            "usage_freq": M["usage_freq"].get(col(h, r, "使う頻度")), "started_at": M["started_at"].get(col(h, r, "使い始め")),
            "shopping_ai_freq": M["shopping_ai_freq"].get(col(h, r, "相談することはありますか")), "device": M["device"].get(col(h, r, "端末"))}

def eligible(h, r):
    if col(h, r, "18歳以上") == "いいえ" or col(h, r, "ログインして") == "いいえ":
        return False, "条件外（18歳未満または未ログイン）"
    chk = col(h, r, "確認の設問")
    if chk and chk != "3":
        return False, "確認の設問が不正解"
    return True, ""

def body_A(h, r):
    items = [{"topic_key": "premise", "link_url": col(h, r, "【お題0", "共有リンク")}]
    for n, key in ((1, "toner"), (2, "credit_card"), (3, "protein")):
        tag = f"【お題{n}"
        items.append({"topic_key": key, "link_url": col(h, r, tag, "共有リンク"), "own_brand_text": col(h, r, tag, "いま使っている"),
                      "intent": M["intent"].get(col(h, r, tag, "予定")), "picked_text": col(h, r, tag, "気になった"), "will_refer": M["will_refer"].get(col(h, r, tag, "参考に"))})
    items = [it for it in items if it["link_url"] and it["link_url"] not in ("なし", "提出しません")]
    return {"source": "crowdworks", "external_id": r[0], "attributes": attributes(h, r), "items": items}

def body_B(h, r):
    g = col(h, r, "何についての相談")
    name = g.split("：", 1)[-1].strip()
    key = GENRE_BY_NAME.get(name)
    item = {"topic_key": "own", "link_url": col(h, r, "共有リンク"), "genre_key": key or "other", "own_category": col(h, r, "その他を選んだ") or name,
            "outcome": M["outcome"].get(col(h, r, "相談した結果")), "chosen_text": col(h, r, "買った（または選んだ）"),
            "appeal_tags": [APPEAL[x] for x in multi(h, r, "魅力に感じた") if x in APPEAL], "rejection_tags": [REJECT[x] for x in multi(h, r, "選ばなかった理由") if x in REJECT],
            "other_text": col(h, r, "補足")}
    return {"source": "crowdworks", "external_id": r[0], "attributes": attributes(h, r), "items": [item] if item["link_url"] else []}

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    if not args:
        print(__doc__); return
    if args[0] == "status":
        r = httpx.get(f"{BASE}/api/admin/import_status", params={"token": token()}, timeout=120); r.raise_for_status()
        WORK.mkdir(exist_ok=True)
        with open(WORK / "cw_status.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["作業ID", "受付数", "不受理数", "処理中", "判定の目安", "内訳"])
            for p in r.json():
                ok = sum(1 for i in p["items"] if i["accept_status"] in ("accepted", "reference")); ng = sum(1 for i in p["items"] if i["accept_status"] == "rejected"); pend = sum(1 for i in p["items"] if i["accept_status"] == "pending")
                core = sum(1 for i in p["items"] if i["topic"] in ("toner", "credit_card", "protein", "own") and i["accept_status"] in ("accepted", "reference"))
                w.writerow([p["external_ref"].split(":", 1)[-1], ok, ng, pend, "承認" if core >= 1 and not pend else ("処理中" if pend else "要確認"),
                            " / ".join(f'{i["topic"]}:{i["accept_status"]}' + (f'（{i["reason"][:20]}）' if i["reason"] else "") for i in p["items"])])
        print(f"work/cw_status.csv に {len(r.json())} 人分を出力")
        return
    kind, path = args[0], args[1]
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    h, data = rows[0], rows[1:]
    sent = 0
    for r in data:
        if not r or not r[0].strip():
            continue
        r = r + [""] * (len(h) - len(r))   # 行が短い場合に備えて列数をそろえる
        ok, why = eligible(h, r)
        if not ok:
            print(r[0], "除外:", why); continue
        body = body_A(h, r) if kind.upper() == "A" else body_B(h, r)
        if dry:
            print(json.dumps(body, ensure_ascii=False)[:600]); continue
        resp = httpx.post(f"{BASE}/api/admin/import", params={"token": token()}, json=body, timeout=120)
        print(r[0], resp.status_code, resp.text[:160]); sent += 1
    print(f"{sent} 人分を送信。数分後に `python tools/import_crowdworks.py status` で結果を確認")

if __name__ == "__main__":
    main()
