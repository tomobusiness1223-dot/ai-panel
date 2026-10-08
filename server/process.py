"""提出1件の処理（仕様書2.4・3章）：取得 → 構造化 → 伏字 → 保存 → 受付判定 → ポイント。"""
from __future__ import annotations
import os, json, hashlib
from sqlalchemy.orm import Session
from .db import SessionLocal, Submission, Topic, Brand, Response, Mention, OwnBrand, PersonalizationSignal, PointLedger, ConversationTurn, Source, now
from pipeline.fetch import fetch_sync
from pipeline.share_html import fetch_share_html
from pipeline.extract import EXTRACT_VERSION, match_prompt, compile_brands, extract_brands, personalization_level, own_brand_lookup, pick_dialog_answer
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

def process_submission(sub_id: int, reprocess: bool = False) -> None:
    """reprocess=True：受付済みの提出を、同じリンクから読み直して作り直す（ポイントは付け直さない）。"""
    db: Session = SessionLocal()
    try:
        sub = db.get(Submission, sub_id)
        if not sub or (sub.accept_status != "pending" and not reprocess):
            return
        if reprocess:
            for r in db.query(Response).filter_by(submission_id=sub.id).all():
                db.query(Mention).filter_by(response_id=r.id).delete()
                db.query(PersonalizationSignal).filter_by(response_id=r.id).delete()
                db.delete(r)
            for T in (OwnBrand, ConversationTurn, Source):
                db.query(T).filter_by(submission_id=sub.id).delete()
            db.flush()
        topic = db.get(Topic, sub.topic_id)
        res = fetch_share_html(sub.link_url)          # まず HTML から（速い）
        if res.status == "error":
            res = fetch_sync(sub.link_url)             # 埋め込みデータが無い形式（s/t_ など）はブラウザで
        if reprocess and res.status != "ok":
            print(f"[process] sub={sub.id} reprocess skipped: fetch={res.status} {res.error}", flush=True)
            db.rollback(); return
        sub.fetch_status = res.status
        print(f"[process] sub={sub.id} fetch={res.status} final={res.final_url} title={res.title!r} msgs={len(res.messages)} err={res.error}", flush=True)
        if res.status != "ok":
            sub.accept_status, sub.reject_reason = "rejected", REJECT_TEXT[res.status]
            db.commit(); return
        mr = match_prompt(topic.prompt_text, res.messages, mode=topic.mode)
        sub.match_status = mr.status
        if reprocess and mr.status in ("modified", "samechat", "fail"):
            db.rollback(); return
        if mr.status in ("modified", "samechat", "fail"):
            # 本文は保存しない（分析に使わない）。差し戻し
            sub.accept_status, sub.reject_reason = "rejected", REJECT_TEXT[mr.status]
            db.commit(); return
        # 保存（原文は暗号化領域、分析には伏字版）
        raw_ref = _save_raw(sub.id, {"url": sub.link_url, "final_url": res.final_url, "title": res.title,
                                     "messages": [m.__dict__ for m in res.messages]})
        brands = [(b.id, b.canonical_name, b.aliases, b.maker) for b in db.query(Brand).filter_by(category_id=topic.category_id)]
        compiled = compile_brands(brands)
        if mr.status == "dialog":
            ans, idx, note = pick_dialog_answer(res.messages, compiled)
            if ans:
                mr.answer, mr.answer_index, mr.note = ans, idx, note
            print(f"[process] sub={sub.id} dialog answer_index={idx} note={note}", flush=True)
        names = llm.find_person_names(mr.answer)
        found = extract_brands(mr.answer, compiled, mention_classifier=llm.classify_mentions if llm.available() else None)
        used_search = any(m.has_citation for m in res.messages if m.role == "assistant")
        resp = Response(submission_id=sub.id, used_search=used_search, raw_text_ref=raw_ref,
                        redacted_text=redact(mr.answer, names), message_count=len(res.messages),
                        extract_status="ok" if any(f.mention_type == "recommended" for f in found) else "none",
                        extract_version=EXTRACT_VERSION, answer_note=mr.note)
        db.add(resp); db.flush()
        cv = llm.VERSION if llm.available() else "rules-v0"
        for f in found:
            db.add(Mention(response_id=resp.id, brand_id=f.brand_id, mention_type=f.mention_type,
                           rank=f.rank if f.mention_type == "recommended" else None, is_numbered=f.is_numbered, classifier_version=cv,
                           product_text=f.product or None, is_primary=f.primary))
        # 個人化の手がかりは、AIからの質問も含めた会話全体で見る（「以前の相談では…」は質問の側に出やすい）
        all_ai = "\n".join(m.text for i, m in enumerate(res.messages) if m.role == "assistant" and i <= max(mr.answer_index, 0))
        lv = llm.classify_personalization(all_ai) or personalization_level(all_ai)
        db.add(PersonalizationSignal(response_id=resp.id, level=lv[0], evidence_text=redact(lv[1], names), classifier_version=cv))
        if sub.own_brand_text:
            db.add(OwnBrand(submission_id=sub.id, brand_id=own_brand_lookup(sub.own_brand_text, compiled), raw_text=sub.own_brand_text))
        # 会話の全発言（伏字済み）と、各回答の参照元
        seen_prompt = False
        for i, m in enumerate(res.messages):
            if m.role == "user":
                kind = "reply" if seen_prompt else "prompt"; seen_prompt = True
            else:
                kind = "answer" if i == mr.answer_index else ("hearing" if i < mr.answer_index else "other")
            db.add(ConversationTurn(submission_id=sub.id, idx=i, role=m.role, kind=kind, redacted_text=redact(m.text, names), n_sources=len(m.sources)))
            for sc in m.sources:
                db.add(Source(submission_id=sub.id, turn_idx=i, is_answer_turn=(i == mr.answer_index), domain=sc["domain"][:120], url=sc["url"], title=sc.get("title")))
        sub.accept_status = "accepted" if mr.status in ("ok", "typo", "dialog") else "reference"
        if not reprocess:
            db.add(PointLedger(panelist_id=sub.panelist_id, delta=topic.point_value, reason="submission", ref_table="submission", ref_id=sub.id))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[process] sub={sub_id} exception {type(e).__name__}: {str(e)[:300]}", flush=True)
        sub = db.get(Submission, sub_id)
        if sub and not reprocess:   # 作り直し中の失敗では、受付済みの状態を変えない
            sub.fetch_status, sub.accept_status, sub.reject_reason = "error", "rejected", REJECT_TEXT["error"] + f"（{type(e).__name__}）"
            db.commit()
    finally:
        db.close()
