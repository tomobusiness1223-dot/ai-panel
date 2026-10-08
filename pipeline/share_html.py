"""ブラウザを使わずに ChatGPT 共有ページの HTML から会話を取り出す。
共有ページは react-router の turbo-stream 形式で会話データを埋め込んでいる（window.__reactRouterContext.streamController.enqueue("...")）。
これを復号して message の列にする。Playwright より速く、小さなサーバーでも動く。"""
from __future__ import annotations
import re, json, httpx
from .fetch import Message, FetchResult

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NULL, UNDEF = -5, -7

def _decode(arr):
    cache = {}
    def d(i):
        if isinstance(i, int) and i < 0:
            return None
        if i in cache:
            return cache[i]
        v = arr[i]
        if isinstance(v, dict):
            out = {}
            cache[i] = out
            for k, vi in v.items():
                out[d(int(k[1:]))] = d(vi)
            return out
        if isinstance(v, list):
            if v and isinstance(v[0], str):  # 型つき値。D/U/B/Y/R は中身が文字列そのもの、S/K/M は索引、P（遅延値）は無視
                tag = v[0]
                if tag in ("D", "U", "B", "Y"):
                    return v[1] if len(v) > 1 else None
                if tag == "R":
                    return v[1] if len(v) > 1 else None
                if tag in ("S", "K"):
                    return [d(x) for x in v[1:]]
                if tag == "M":
                    return {d(v[j]): d(v[j + 1]) for j in range(1, len(v) - 1, 2)}
                if tag == "N" and len(v) > 1 and isinstance(v[1], dict):
                    return {d(int(k[1:])): d(vi) for k, vi in v[1].items()}
                return None
            out = []
            cache[i] = out
            out.extend(d(x) for x in v)
            return out
        return v
    return d(0)

def parse_share_html(html: str) -> dict | None:
    chunks = re.findall(r'streamController\.enqueue\((".*?")\);', html, flags=re.S)
    for c in chunks:
        try:
            s = json.loads(c)
            arr = json.loads(s)
        except Exception:
            continue
        if not isinstance(arr, list) or not arr:
            continue
        root = _decode(arr)
        if not isinstance(root, dict):
            continue
        ld = root.get("loaderData") or {}
        for k, v in ld.items():
            if not isinstance(v, dict):
                continue
            if isinstance(v.get("serverResponse"), dict):          # 会話全体の共有（/share/）
                data = v["serverResponse"].get("data")
                if isinstance(data, dict) and "mapping" in data:
                    return data
            post = ((v.get("postWithProfile") or {}).get("post")) if isinstance(v.get("postWithProfile"), dict) else None
            if isinstance(post, dict):                               # 1回答だけの共有（/s/t_）
                msgs = []
                for att in post.get("attachments") or []:
                    if isinstance(att, dict):
                        msgs.extend(m for m in (att.get("messages") or []) if isinstance(m, dict))
                if msgs:
                    return {"post_messages": msgs, "title": post.get("text") or ""}
    return None

TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>")
ENTITY = re.compile(r"<Entity\b([^<>]*)/?>")

def collect_bindings(data: dict) -> dict:
    """商品カードの参照（turnNNNproductN）→ 商品情報。会話内のどの発言に付いていても拾う。"""
    out = {}
    for node in (data.get("mapping") or {}).values():
        meta = ((node or {}).get("message") or {}).get("metadata") or {}
        b = (((meta.get("model_dil_v2") or {}).get("appData") or {}).get("opGenui") or {}).get("modelDataBindings")
        if isinstance(b, dict):
            out.update({k: v for k, v in b.items() if isinstance(v, dict)})
    return out

def clean_markup(text: str, bindings: dict | None = None) -> str:
    """ChatGPT の表示用タグ（<box> <text> <Entity> <Cite> など）を外し、商品名を埋め戻す。"""
    bindings = bindings or {}
    def ent(m):
        attrs = m.group(1)
        v = re.search(r'value="([^"]*)"', attrs)
        if v:
            return v.group(1)
        r = re.search(r"value=\{([A-Za-z0-9_]+)\.title\}", attrs) or re.search(r'ref="([A-Za-z0-9_]+)"', attrs)
        if r and r.group(1) in bindings:
            return str(bindings[r.group(1)].get("title") or "")
        return ""
    t = ENTITY.sub(ent, text)
    t = TAG.sub("", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()

def _sources(meta: dict) -> list[dict]:
    """その発言が検索で参照したページ（ドメイン・URL・題名）。"""
    out, seen = [], set()
    for g in meta.get("search_result_groups") or []:
        if not isinstance(g, dict):
            continue
        for en in g.get("entries") or []:
            url = (en or {}).get("url") or ""
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            out.append({"domain": g.get("domain") or httpx.URL(url).host, "url": url.split("?utm_source=")[0], "title": ((en.get("title") or "")[:200])})
    return out

def _message_to_text(m: dict, bindings: dict | None = None) -> tuple[str, str, bool] | None:
    role = ((m.get("author") or {}).get("role")) or ""
    if role not in ("user", "assistant"):
        return None
    meta = m.get("metadata") or {}
    if meta.get("is_visually_hidden_from_conversation"):
        return None
    content = m.get("content") or {}
    ctype = content.get("content_type")
    parts = content.get("parts") or []
    text = "\n".join(p for p in parts if isinstance(p, str)).strip() if ctype in ("text", "multimodal_text") else ""
    if not text:
        return None
    has_cite = bool(meta.get("citations") or meta.get("content_references") or meta.get("search_result_groups"))
    # 本文に埋め込まれた制御マーカー：\ue200 種類 \ue202 中身 \ue201（引用 cite、商品カルーセル products など）
    if "\ue200" in text:
        if "\ue200cite" in text:
            has_cite = True
        text = re.sub(r"\ue200[^\ue200-\ue202]*(?:\ue202[^\ue200\ue201]*)*\ue201", "", text, flags=re.S)
    text = re.sub(r"[\ue000-\uf8ff]", "", text)
    if role == "assistant":
        if "<Cite" in text:
            has_cite = True
        text = clean_markup(text, bindings)
    if not text:
        return None
    return role, text, has_cite

def messages_from_data(data: dict) -> list[Message]:
    if "post_messages" in data:
        out = []
        for m in data["post_messages"]:
            r = _message_to_text(m)
            if r:
                out.append(Message(*r))
        return out
    mapping = data.get("mapping") or {}
    bindings = collect_bindings(data)
    # current_node から親をたどって順番を復元。無ければ create_time 順
    order = []
    node = data.get("current_node")
    seen = set()
    while node and node in mapping and node not in seen:
        seen.add(node); order.append(node); node = mapping[node].get("parent")
    order.reverse()
    if not order:
        order = sorted(mapping, key=lambda k: (mapping[k].get("message") or {}).get("create_time") or 0)
    msgs = []
    pending_sources: list[dict] = []   # 検索の途中経過（本文なしの発言）に付いた参照元は、次の回答に付ける
    for nid in order:
        m = (mapping.get(nid) or {}).get("message")
        if not m:
            continue
        role = ((m.get("author") or {}).get("role")) or ""
        src = _sources(m.get("metadata") or {}) if role in ("assistant", "tool") else []
        r = _message_to_text(m, bindings)
        if r:
            msg = Message(*r)
            msg.sources = pending_sources + src if role == "assistant" else []
            if role == "assistant":
                pending_sources = []
            msgs.append(msg)
        else:
            pending_sources += src
    # 連続する assistant の発言（検索の途中経過など）は1つにまとめる
    merged: list[Message] = []
    for m in msgs:
        if merged and merged[-1].role == m.role == "assistant":
            merged[-1].text += "\n" + m.text
            merged[-1].has_citation = merged[-1].has_citation or m.has_citation
            merged[-1].sources += m.sources
        else:
            merged.append(m)
    for m in merged:   # 重複を除く
        seen, uniq = set(), []
        for x in m.sources:
            if x["url"] not in seen:
                seen.add(x["url"]); uniq.append(x)
        m.sources = uniq
        if uniq:
            m.has_citation = True
    return merged

def fetch_share_html(url: str, timeout: float = 30, tries: int = 3) -> FetchResult:
    """一時的な失敗（5xx・429・通信エラー）は間をあけて再試行する。404 とトップへの転送だけを「無効なリンク」とする。"""
    import time
    last = FetchResult("error", [], error="not tried")
    for attempt in range(tries):
        if attempt:
            time.sleep(2 * attempt)
        try:
            r = httpx.get(url, headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"}, follow_redirects=True, timeout=timeout)
        except Exception as e:
            last = FetchResult("error", [], error=f"http: {type(e).__name__}: {str(e)[:200]}")
            continue
        final = str(r.url)
        if r.status_code in (404, 410):
            return FetchResult("invalid", [], final, error=f"http {r.status_code}")
        if r.status_code != 200:
            last = FetchResult("error", [], final, error=f"http {r.status_code}")
            continue
        if httpx.URL(final).path in ("", "/"):
            return FetchResult("invalid", [], final, error="redirected to top")
        data = parse_share_html(r.text)
        if not data:   # 会話データが無いページ（存在しないリンクなど）。再試行せず、ブラウザでの確認に回す
            return FetchResult("error", [], final, error="no embedded data")
        msgs = messages_from_data(data)
        if not msgs:
            return FetchResult("invalid", [], final, error="no messages in data")
        return FetchResult("ok", msgs, final, data.get("title") or "", r.text)
    return last
