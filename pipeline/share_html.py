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

def _message_to_text(m: dict) -> tuple[str, str, bool] | None:
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
    text = re.sub(r"[\ue000-\uf8ff][^\ue000-\uf8ff]{0,200}?[\ue000-\uf8ff]", "", text)
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
    for nid in order:
        m = (mapping.get(nid) or {}).get("message")
        if not m:
            continue
        r = _message_to_text(m)
        if r:
            msgs.append(Message(*r))
    # 連続する assistant の発言（検索の途中経過など）は、質問への答えとして最後のものを残すのではなく結合
    merged: list[Message] = []
    for m in msgs:
        if merged and merged[-1].role == m.role == "assistant":
            merged[-1].text += "\n" + m.text
            merged[-1].has_citation = merged[-1].has_citation or m.has_citation
        else:
            merged.append(Message(m.role, m.text, m.has_citation))
    return merged

def fetch_share_html(url: str, timeout: float = 30) -> FetchResult:
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"}, follow_redirects=True, timeout=timeout)
    except Exception as e:
        return FetchResult("error", [], error=f"http: {type(e).__name__}: {str(e)[:200]}")
    final = str(r.url)
    if r.status_code != 200:
        return FetchResult("invalid", [], final, error=f"http {r.status_code}")
    if httpx.URL(final).path in ("", "/"):
        return FetchResult("invalid", [], final, error="redirected to top")
    data = parse_share_html(r.text)
    if not data:
        return FetchResult("error", [], final, error="no embedded data")
    msgs = messages_from_data(data)
    if not msgs:
        return FetchResult("invalid", [], final, error="no messages in data")
    return FetchResult("ok", msgs, final, data.get("title") or "", r.text)
