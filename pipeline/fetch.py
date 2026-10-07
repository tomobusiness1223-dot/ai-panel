"""ChatGPT 共有ページから発言を取り出す（仕様書3.1）。
検証実験で確認した方法：ページ内の [data-message-author-role] 要素を発言ごとに読む。
"""
from __future__ import annotations
import re, asyncio, dataclasses
from urllib.parse import urlparse

SHARE = re.compile(r"^https://chatgpt\.com/share/[0-9a-f-]{20,}/?$")
S_T = re.compile(r"^https://chatgpt\.com/s/t_[A-Za-z0-9_-]{6,}/?$")
C_URL = re.compile(r"^https://chatgpt\.com/(c|g)/")
S_OTHER = re.compile(r"^https://chatgpt\.com/s/(p_|cx_)")

def classify_link(url: str) -> tuple[str, str | None]:
    """(link_type, reject_reason)。share / s_t は受け付ける。"""
    u = url.strip()
    if SHARE.match(u):
        return "share", None
    if S_T.match(u):
        return "s_t", None
    if C_URL.match(u):
        return "c", "それは本人しか開けないチャットのURLです。画面右上の「共有」から共有リンクを作り、そのURL（chatgpt.com/share/… で始まる）を貼ってください。"
    if S_OTHER.match(u):
        return "other", "そのリンクは外部から開けない形式です。会話全体の「共有」で作ったリンク（chatgpt.com/share/…）を貼ってください。"
    host = urlparse(u).netloc
    if host and "chatgpt.com" not in host and "openai.com" not in host:
        return "other", "ChatGPT の共有リンクではありません（chatgpt.com/share/… で始まるURL）。"
    return "other", "共有リンクの形式が違います。chatgpt.com/share/… で始まるURLを貼ってください。"

@dataclasses.dataclass
class Message:
    role: str
    text: str
    has_citation: bool = False
    widget_text: str = ""

@dataclasses.dataclass
class FetchResult:
    status: str                # ok / invalid / error
    messages: list[Message]
    final_url: str = ""
    title: str = ""
    html: str = ""
    error: str = ""

JS_EXTRACT = r"""
() => {
  const nodes = Array.from(document.querySelectorAll('[data-message-author-role]'));
  return nodes.map(n => {
    const role = n.getAttribute('data-message-author-role');
    const clone = n.cloneNode(true);
    // ショッピングのカルーセル（商品カードの帯）は本文から外し、別に残す
    let widget = '';
    const metas = Array.from(clone.querySelectorAll('[data-shopping-browse-product-metadata], [data-testid^="shopping-product"]'));
    const total = (clone.innerText || '').length;
    const removed = new Set();
    for (const m of metas) {
      let el = m;
      while (el.parentElement && el.parentElement !== clone && (el.parentElement.innerText || '').length < total * 0.6) el = el.parentElement;
      if (!removed.has(el)) { removed.add(el); widget += (el.innerText || '') + '\n'; el.remove(); }
    }
    const cites = n.querySelectorAll('[data-testid="webpage-citation-pill"], a[href*="utm_source=chatgpt.com"], [data-content-reference-start]');
    return {role, text: clone.innerText, widget_text: widget, has_citation: cites.length > 0};
  });
}
"""

async def fetch_share_page(url: str, timeout_ms: int = 20000) -> FetchResult:
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(locale="ja-JP", user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                # 無効なリンクはトップページへ飛ばされる
                try:
                    await page.wait_for_selector("[data-message-author-role]", timeout=timeout_ms)
                except Exception:
                    path = urlparse(page.url).path
                    if path in ("", "/") or "auth" in page.url:
                        return FetchResult("invalid", [], page.url, await page.title())
                    return FetchResult("invalid", [], page.url, await page.title(), error="no messages")
                await page.wait_for_timeout(800)  # 遅延描画分
                items = await page.evaluate(JS_EXTRACT)
                html = await page.content()
                msgs = [Message(i["role"], i["text"].strip(), bool(i["has_citation"]), (i.get("widget_text") or "").strip()) for i in items if i["text"].strip()]
                return FetchResult("ok" if msgs else "invalid", msgs, page.url, await page.title(), html)
            finally:
                await browser.close()
    except Exception as e:  # ネットワーク・起動失敗
        return FetchResult("error", [], error=str(e)[:300])

def fetch_sync(url: str, retries: int = 2) -> FetchResult:
    last = None
    for _ in range(retries + 1):
        last = asyncio.run(fetch_share_page(url))
        if last.status != "error":
            return last
    return last
