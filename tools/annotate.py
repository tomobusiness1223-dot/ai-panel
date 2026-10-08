"""運営用：注釈の取り出しと書き戻し（サブスク内の Claude でまとめて処理する運用）。

  python tools/annotate.py pull          → work/pending.json に、注釈がまだ無い提出（伏字済みの会話つき）を保存
  python tools/annotate.py push          → work/annotations.json を本番に書き戻す
  python tools/annotate.py export NAME   → work/NAME.csv（participants / mentions / products / turns / sources / conditions / new_brands ...）

管理用トークンは環境変数 ADMIN_TOKEN、または .admin_token ファイル（git に入れない）から読む。トークンは表示しない。
"""
import sys, os, json, pathlib, httpx
ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = os.environ.get("PANEL_BASE", "https://ai-panel-z5nr.onrender.com")
WORK = ROOT / "work"

def token() -> str:
    t = os.environ.get("ADMIN_TOKEN")
    f = ROOT / ".admin_token"
    if not t and f.exists():
        t = f.read_text().strip()
    if not t:
        sys.exit("管理用トークンがありません。Render の Environment にある ADMIN_TOKEN の値を、ai-panel/.admin_token に保存してください。")
    return t

def main():
    WORK.mkdir(exist_ok=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "pull":
        r = httpx.get(f"{BASE}/api/admin/pending", params={"token": token(), "limit": int(sys.argv[2]) if len(sys.argv) > 2 else 50}, timeout=120)
        r.raise_for_status(); d = r.json()
        (WORK / "pending.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
        print(f"pending: {len(d['items'])} 件を work/pending.json に保存（未処理 合計 {d['remaining']}）")
    elif cmd == "push":
        body = json.loads((WORK / "annotations.json").read_text())
        r = httpx.post(f"{BASE}/api/admin/annotations", params={"token": token()}, json=body, timeout=120)
        print(r.status_code, r.text[:300])
    elif cmd == "export":
        name = sys.argv[2]
        r = httpx.get(f"{BASE}/api/admin/export/{name}", params={"token": token(), "scope": sys.argv[3] if len(sys.argv) > 3 else "wide"}, timeout=120)
        r.raise_for_status(); (WORK / f"{name}.csv").write_text(r.text)
        print(f"work/{name}.csv: {max(0, r.text.count(chr(10)) - 1)} 行")
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
