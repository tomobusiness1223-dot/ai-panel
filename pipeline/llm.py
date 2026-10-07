"""LLM を使う分類（言及の種類・個人化の手がかり・人名検出）。ANTHROPIC_API_KEY があるときだけ使う。無ければ規則版にフォールバック。"""
from __future__ import annotations
import os, json, httpx
MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5-5")
KEY = os.environ.get("ANTHROPIC_API_KEY")
VERSION = "llm-v1"

def available() -> bool:
    return bool(KEY)

def _ask(system: str, user: str, max_tokens: int = 800) -> str:
    r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
        "x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": user}]})
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])

def classify_mentions(text: str, found):
    """found: list[Found]。各ブランドを recommended / compared / negative / unknown に分類して返す。"""
    if not KEY or not found:
        return found
    names = [f.name for f in found]
    sys_ = ("あなたは商品推薦文の分類器です。与えられたAIの回答文の中で、各ブランドがどう扱われているかを分類します。"
            "recommended=おすすめとして挙げている / compared=説明のために引き合いに出しただけ / negative=勧めないと述べている / unknown=判断できない。"
            "JSONだけを返す: {\"ブランド名\": \"分類\", ...}")
    try:
        out = _ask(sys_, f"回答文:\n{text[:6000]}\n\nブランド: {json.dumps(names, ensure_ascii=False)}")
        j = json.loads(out[out.index("{"): out.rindex("}") + 1])
        for f in found:
            v = j.get(f.name)
            if v in ("recommended", "compared", "negative", "unknown"):
                f.mention_type = v
    except Exception:
        pass
    return found

def classify_personalization(text: str) -> tuple[str, str] | None:
    if not KEY:
        return None
    sys_ = ("AIの回答文に、回答相手本人の情報が使われている手がかりがあるかを分類します。"
            "strong=名前・過去の会話・本人の具体的な事実（家族構成、使用端末、職業、体質など）への言及 / weak=口調や例示の偏りなど間接的 / none=なし。"
            "JSONだけを返す: {\"level\": \"strong|weak|none\", \"evidence\": \"該当箇所の短い引用\"}")
    try:
        out = _ask(sys_, text[:6000], 300)
        j = json.loads(out[out.index("{"): out.rindex("}") + 1])
        if j.get("level") in ("strong", "weak", "none"):
            return j["level"], (j.get("evidence") or "")[:300]
    except Exception:
        return None
    return None

def find_person_names(text: str) -> list[str]:
    if not KEY:
        return []
    try:
        out = _ask("文中に出てくる個人の名前（姓・名・ニックネーム）だけをJSON配列で返す。なければ []。", text[:6000], 200)
        return [s for s in json.loads(out[out.index("["): out.rindex("]") + 1]) if isinstance(s, str)]
    except Exception:
        return []
