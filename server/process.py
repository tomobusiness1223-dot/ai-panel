"""提出1件の処理（仕様書2.4・3章）：取得 → 構造化 → 伏字 → 保存 → 受付判定 → ポイント。"""
from __future__ import annotations
import os, json, hashlib
from sqlalchemy.orm import Session
from .db import SessionLocal, Submission, Topic, Brand, Response, Mention, OwnBrand, PersonalizationSignal, PointLedger, now
from pipeline.fetch import fetch_sync
from pipeline.extract import match_prompt, compile_brands, extract_brands, personalization_level, own_brand_lookup
from pipeline.redact import redact
from pipeline import llm

RAW_DIR = "data/raw"
REJECT_TEXT = {
    "invalid": "共有リンクを開けませんでした。会話が削除されたか、共有が解除されています。もう一度「共有」からリンクを作り直して貼ってください。",
    "error": "取得に失敗しました。しばらくしてから再提出してください。",
    "modified": "質問文が指定と違います（付け足しや変更があります）。新しいチャットで、コピーした質問文をそのまま送ってください。長い回答でも大丈夫です。",
    "samechat": "同じチャットで別の質問が先にありました。お題ごとに新しいチャットを開いて、質問文だけを送ってください。",
    "fail": "指定の質問とその回答が見つかりませんでした。新しいチャットで質問文をそのまま送り、その会話を共有してください。",
}

def _save_raw(sub_id: int, payload: dict) -> str:
    os.makedirs(RAW_DIR, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False).encode()
    key = os.environ.get("RAW_KEY")
    path = f"{RAW_DIR}/{sub_id}.json"
    if key:
        from cryptography.fernet import Fernet
        data = Fernet(key).encrypt(data); path += ".enc"
    with open(path, "wb") as f:
        f.write(data)
    return path

def process_submission(sub_id: int) -> None:
    db: Session = SessionLocal()
    try:
        sub = db.get(Submission, sub_id)
        if not sub or sub.accept_status != "pending":
            return
        topic = db.get(Topic, sub.topic_id)
        res = fetch_sync(sub.link_url)
        sub.fetch_status = res.status
        if res.status != "ok":
            sub.accept_status, sub.reject_reason = "rejected", REJECT_TEXT[res.status]
            db.commit(); return
        mr = match_prompt(topic.prompt_text, res.messages)
        sub.match_status = mr.status
        if mr.status in ("modified", "samechat", "fail"):
            # 本文は保存しない（分析に使わない）。差し戻し
            sub.accept_status, sub.reject_reason = "rejected", REJECT_TEXT[mr.status]
            db.commit(); return
        # 保存（原文は暗号化領域、分析には伏字版）
        names = llm.find_person_names(mr.answer)
        raw_ref = _save_raw(sub.id, {"url": sub.link_url, "final_url": res.final_url, "title": res.title,
                                     "messages": [m.__dict__ for m in res.messages]})
        brands = [(b.id, b.canonical_name, b.aliases) for b in db.query(Brand).filter_by(category_id=topic.category_id)]
        compiled = compile_brands(brands)
        found = extract_brands(mr.answer, compiled, mention_classifier=llm.classify_mentions if llm.available() else None)
        used_search = any(m.has_citation for m in res.messages if m.role == "assistant")
        resp = Response(submission_id=sub.id, used_search=used_search, raw_text_ref=raw_ref,
                        redacted_text=redact(mr.answer, names), message_count=len(res.messages),
                        extract_status="ok" if any(f.mention_type == "recommended" for f in found) else "none")
        db.add(resp); db.flush()
        cv = llm.VERSION if llm.available() else "rules-v0"
        for f in found:
            db.add(Mention(response_id=resp.id, brand_id=f.brand_id, mention_type=f.mention_type,
                           rank=f.rank if f.mention_type == "recommended" else None, is_numbered=f.is_numbered, classifier_version=cv))
        lv = llm.classify_personalization(mr.answer) or personalization_level(mr.answer)
        db.add(PersonalizationSignal(response_id=resp.id, level=lv[0], evidence_text=redact(lv[1], names), classifier_version=cv))
        if sub.own_brand_text:
            db.add(OwnBrand(submission_id=sub.id, brand_id=own_brand_lookup(sub.own_brand_text, compiled), raw_text=sub.own_brand_text))
        sub.accept_status = "accepted" if mr.status in ("ok", "typo") else "reference"
        db.add(PointLedger(panelist_id=sub.panelist_id, delta=topic.point_value, reason="submission", ref_table="submission", ref_id=sub.id))
        db.commit()
    except Exception as e:
        db.rollback()
        sub = db.get(Submission, sub_id)
        if sub:
            sub.fetch_status, sub.accept_status, sub.reject_reason = "error", "rejected", REJECT_TEXT["error"] + f"（{type(e).__name__}）"
            db.commit()
    finally:
        db.close()
