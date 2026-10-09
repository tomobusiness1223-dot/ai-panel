"""提出1件の処理（仕様書2.4・3章）：取得 → 構造化 → 伏字 → 保存 → 受付判定 → ポイント。"""
from __future__ import annotations
import os, json, hashlib, threading
_PROCESS_LOCK = threading.Lock()   # 取り込みで多数の提出が同時に来ても、処理は1件ずつ（SQLite の書き込み衝突を避ける）
from sqlalchemy.orm import Session
from sqlalchemy import select
from .db import SessionLocal, Panelist, Submission, Topic, Category, Brand, Response, Mention, OwnBrand, PersonalizationSignal, PointLedger, ConversationTurn, Source, now
from pipeline.fetch import fetch_sync
from pipeline.share_html import fetch_share_html
from pipeline.extract import EXTRACT_VERSION, MatchResult, match_prompt, compile_brands, extract_brands, personalization_level, own_brand_lookup, pick_dialog_answer, stage_info
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
    with _PROCESS_LOCK:
        _process_submission(sub_id, reprocess)

def _process_submission(sub_id: int, reprocess: bool = False) -> None:
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
        res = fetch_share_html(sub.link_url)          # HTML の埋め込みデータから読む（share/ と s/t_ の両方に対応）
        if res.status == "error" and os.environ.get("USE_BROWSER_FALLBACK") == "1":
            res = fetch_sync(sub.link_url)             # ブラウザでの取得は開発時のみ（本番の小さなサーバーではメモリ不足で落ちる）
        if res.status == "error" and "no embedded data" in (res.error or ""):
            res.status = "invalid"                     # 会話データの無いページ＝開けない共有リンクとして扱う
        if reprocess and res.status != "ok":
            print(f"[process] sub={sub.id} reprocess skipped: fetch={res.status} {res.error}", flush=True)
            db.rollback(); return
        sub.fetch_status = res.status
        print(f"[process] sub={sub.id} fetch={res.status} final={res.final_url} title={res.title!r} msgs={len(res.messages)} err={res.error}", flush=True)
        if res.status != "ok":
            sub.accept_status, sub.reject_reason = "rejected", REJECT_TEXT[res.status]
            db.commit(); return
        if topic.mode == "own":
            # 系統B：質問文の指定なし。会話全体を使い、結論の回答を分析対象にする
            mr = MatchResult("own", "", -1, "本人の実会話。質問文の一致判定なし")
            assistants = [(i, m) for i, m in enumerate(res.messages) if m.role == "assistant"]
            if assistants:
                mr.answer, mr.answer_index = assistants[-1][1].text, assistants[-1][0]
        elif topic.mode == "premise":
            mr = match_prompt(topic.prompt_text, res.messages, mode="dialog")
            if mr.status == "followup":
                mr.status = "dialog"
        else:
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
        cat_id = topic.category_id
        if topic.mode == "own":
            # 本人が選んだカテゴリの辞書を使う（お題のカテゴリ名と一致すれば）
            c = db.scalar(select(Category).where((Category.key == (sub.genre_key or "")) | (Category.name == (sub.own_category or ""))))
            cat_id = c.id if c else None
        brands = [(b.id, b.canonical_name, b.aliases, b.maker) for b in db.query(Brand).filter_by(category_id=cat_id)] if cat_id else []
        compiled = compile_brands(brands)
        if topic.mode == "premise":
            compiled = []   # 前提の回答にブランドは数えない
        if mr.status in ("dialog", "own") and compiled:
            ans, idx, note = pick_dialog_answer(res.messages, compiled)
            if ans:
                mr.answer, mr.answer_index, mr.note = ans, idx, note
            print(f"[process] sub={sub.id} dialog answer_index={idx} note={note}", flush=True)
        st = stage_info(res.messages, compiled) if (topic.mode == "dialog" and compiled) else None
        if st and st["early"]:
            # AI が質問しているのに、答える前に共有された。LINE ではその場で出し直してもらう。取り込み（クラウドワークス）は出し直せないので参考扱い
            pan = db.get(Panelist, sub.panelist_id)
            if not reprocess and (pan is None or (pan.source or "line") == "line"):
                sub.match_status, sub.accept_status = "early", "rejected"
                sub.reject_reason = "ChatGPT からの質問に答える前に共有されています。同じチャットで質問に答え、答えを踏まえたおすすめが出てから、もう一度「共有」でリンクを作り直して貼ってください。（質問に答えたくない場合は「おまかせします。おすすめを教えてください。」と送ってください）"
                db.commit(); return
            mr.status, mr.note = "early", (mr.note or "") + "／質問に答える前に共有（最初の推薦のみ）"
        names = llm.find_person_names(mr.answer)
        found = extract_brands(mr.answer, compiled, mention_classifier=llm.classify_mentions if llm.available() else None)
        used_search = any(m.has_citation for m in res.messages if m.role == "assistant")
        resp = Response(submission_id=sub.id, used_search=used_search, raw_text_ref=raw_ref,
                        redacted_text=redact(mr.answer, names), message_count=len(res.messages),
                        extract_status="ok" if any(f.mention_type == "recommended" for f in found) else "none",
                        extract_version=EXTRACT_VERSION, answer_note=mr.note,
                        n_stages=st["n_stages"] if st else None, first_index=st["first"] if st else None)
        db.add(resp); db.flush()
        cv = llm.VERSION if llm.available() else "rules-v0"
        for f in found:
            db.add(Mention(response_id=resp.id, brand_id=f.brand_id, mention_type=f.mention_type,
                           rank=f.rank if f.mention_type == "recommended" else None, is_numbered=f.is_numbered, classifier_version=cv,
                           product_text=f.product or None, is_primary=f.primary))
        # 最初の回答（質問される前＝前提だけの推薦）の推薦も、stage="first" として残す
        if st and st["first"] is not None:
            for f in extract_brands(res.messages[st["first"]].text, compiled, mention_classifier=llm.classify_mentions if llm.available() else None):
                if f.mention_type == "recommended":
                    db.add(Mention(response_id=resp.id, brand_id=f.brand_id, mention_type=f.mention_type, rank=f.rank, is_numbered=f.is_numbered,
                                   classifier_version=cv, product_text=f.product or None, is_primary=f.primary, stage="first"))
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
                if i == mr.answer_index:
                    kind = "answer"
                elif i < mr.answer_index:   # 結論より前：推薦を含めば途中の一覧、含まなければヒアリング
                    kind = "interim" if sum(1 for f in extract_brands(m.text, compiled) if f.mention_type == "recommended" and f.is_numbered) >= 2 else "hearing"
                else:
                    kind = "other"
            db.add(ConversationTurn(submission_id=sub.id, idx=i, role=m.role, kind=kind, redacted_text=redact(m.text, names), n_sources=len(m.sources)))
            for sc in m.sources:
                db.add(Source(submission_id=sub.id, turn_idx=i, is_answer_turn=(i == mr.answer_index), domain=sc["domain"][:120], url=sc["url"], title=sc.get("title")))
        ok_status = ("ok", "typo", "dialog", "own") + (("fallback",) if topic.mode == "dialog" else ())   # 2段階の質問文では「おまかせします」も正規の進め方
        sub.accept_status = "accepted" if mr.status in ok_status else "reference"
        if not reprocess:
            db.add(PointLedger(panelist_id=sub.panelist_id, delta=sub.point_value or topic.point_value, reason="submission", ref_table="submission", ref_id=sub.id))
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
