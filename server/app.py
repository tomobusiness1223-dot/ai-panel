"""API サーバー（仕様書2章・6章）。LIFF 画面は / で配信。"""
from __future__ import annotations
import os, hashlib, datetime as dt, pathlib, secrets, string
os.chdir(pathlib.Path(__file__).resolve().parent.parent)  # data/ と liff/ を相対パスで使うため
if os.path.exists(".env"):  # .env の値は、すでにある環境変数を上書きしない
    for _line in open(".env", encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1); os.environ.setdefault(_k.strip(), _v.strip())
from fastapi import FastAPI, Depends, HTTPException, Header, BackgroundTasks, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session
import httpx
from .db import (SessionLocal, init_db, Panelist, PanelistAttribute, Category, Brand, Topic, Submission,
                 Response, Mention, OwnBrand, PersonalizationSignal, PointLedger, ConversationTurn, Source, Annotation, SubmissionReaction, OwnAnswer, now)
from .process import process_submission
from pipeline.fetch import classify_link

DEV_MODE = os.environ.get("DEV_MODE", "1") == "1"
LINE_CHANNEL_ID = os.environ.get("LINE_CHANNEL_ID")
LIFF_ID = os.environ.get("LIFF_ID", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin")
CONSENT_VERSION = "v1"

app = FastAPI(title="ai-panel")
init_db()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _enc(uid: str) -> str:
    key = os.environ.get("RAW_KEY")
    if not key:
        return uid
    from cryptography.fernet import Fernet
    return Fernet(key).encrypt(uid.encode()).decode()

async def line_user_id(request: Request, authorization: str | None = Header(default=None), x_dev_uid: str | None = Header(default=None)) -> str:
    """LINE の ID トークンを検証して userId を返す。開発モードでは X-Dev-Uid を信用する。"""
    if authorization and authorization.startswith("Bearer ") and LINE_CHANNEL_ID:
        token = authorization[7:]
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api.line.me/oauth2/v2.1/verify", data={"id_token": token, "client_id": LINE_CHANNEL_ID})
        if r.status_code != 200:
            raise HTTPException(401, "LINE のログインを確認できませんでした")
        return r.json()["sub"]
    if DEV_MODE and x_dev_uid:
        return "dev:" + x_dev_uid
    raise HTTPException(401, "ログインが必要です")

def get_panelist(db: Session, uid: str) -> Panelist | None:
    h = hashlib.sha256(uid.encode()).hexdigest()
    return db.scalar(select(Panelist).where(Panelist.line_user_hash == h))

@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    """稼働確認用。件数だけ返す（個人の情報は含まない）。"""
    return {"ok": True, "panelists": db.scalar(select(func.count(Panelist.id))), "submissions": db.scalar(select(func.count(Submission.id))),
            "accepted": db.scalar(select(func.count(Submission.id)).where(Submission.accept_status.in_(["accepted", "reference"]))),
            "turns": db.scalar(select(func.count(ConversationTurn.id))), "sources": db.scalar(select(func.count(Source.id))),
            "mentions": db.scalar(select(func.count(Mention.id)).where(Mention.mention_type == "recommended")),
            "annotated": db.scalar(select(func.count(Annotation.id)).where(Annotation.kind == "done")),
            "reactions": db.scalar(select(func.count(SubmissionReaction.id))), "own": db.scalar(select(func.count(OwnAnswer.id))),
            "declined": db.scalar(select(func.count(Submission.id)).where(Submission.accept_status == "declined"))}

# ---------- 画面用 ----------
@app.get("/api/config")
def config():
    return {"liffId": LIFF_ID, "devMode": DEV_MODE, "consentVersion": CONSENT_VERSION}

class RegisterIn(BaseModel):
    gender: str | None = None
    birth_year: int | None = None
    prefecture: str | None = None
    household: str | None = None
    children: str | None = None
    ai_plan: str | None = None
    memory_setting: str | None = None
    usage_freq: str | None = None
    started_at: str | None = None
    device: str | None = None
    consent: bool = False

@app.post("/api/register")
def register(body: RegisterIn, uid: str = Depends(line_user_id), db: Session = Depends(get_db)):
    if not body.consent:
        raise HTTPException(400, "利用目的への同意が必要です")
    p = get_panelist(db, uid)
    if not p:
        p = Panelist(line_user_hash=hashlib.sha256(uid.encode()).hexdigest(), line_user_id_enc=_enc(uid), consent_version=CONSENT_VERSION)
        db.add(p); db.flush()
    # 以前の属性を閉じて履歴にする
    for a in db.scalars(select(PanelistAttribute).where(PanelistAttribute.panelist_id == p.id, PanelistAttribute.valid_to.is_(None))):
        a.valid_to = now()
    db.add(PanelistAttribute(panelist_id=p.id, **body.model_dump(exclude={"consent"})))
    if not p.completion_code:
        p.completion_code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    db.commit()
    return {"ok": True, "panelist_id": p.id}

@app.get("/api/me")
def me(uid: str = Depends(line_user_id), db: Session = Depends(get_db)):
    p = get_panelist(db, uid)
    if not p:
        return {"registered": False}
    attr = db.scalar(select(PanelistAttribute).where(PanelistAttribute.panelist_id == p.id, PanelistAttribute.valid_to.is_(None)))
    points = db.scalar(select(func.coalesce(func.sum(PointLedger.delta), 0)).where(PointLedger.panelist_id == p.id))
    subs = db.scalars(select(Submission).where(Submission.panelist_id == p.id).order_by(Submission.id.desc())).all()
    t = now()
    req = db.scalars(select(Topic.id).where(Topic.required.is_(True), Topic.opens_at <= t, (Topic.closes_at.is_(None)) | (Topic.closes_at > t))).all()
    done_ids = {s.topic_id for s in subs if s.accept_status in ("accepted", "reference", "declined")}
    all_done = bool(req) and all(tid in done_ids for tid in req)
    if not p.completion_code:
        p.completion_code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6)); db.commit()
    return {"registered": True, "panelist_id": p.id, "points": points, "completion_code": p.completion_code if all_done else None,
            "required_total": len(req), "required_done": sum(1 for tid in req if tid in done_ids),
            "attributes": {k: getattr(attr, k) for k in RegisterIn.model_fields if k != "consent"} if attr else None,
            "submissions": [{"id": s.id, "topic_id": s.topic_id, "accept_status": s.accept_status, "reject_reason": s.reject_reason, "submitted_at": s.submitted_at.isoformat()} for s in subs]}

@app.get("/api/topics")
def topics(uid: str = Depends(line_user_id), db: Session = Depends(get_db)):
    p = get_panelist(db, uid)
    t = now()
    rows = db.execute(select(Topic, Category).join(Category, Topic.category_id == Category.id)
                      .where(Topic.opens_at <= t, (Topic.closes_at.is_(None)) | (Topic.closes_at > t)).order_by(Topic.id)).all()
    done = {}
    if p:
        for s in db.scalars(select(Submission).where(Submission.panelist_id == p.id)):
            if s.accept_status in ("accepted", "reference", "pending", "declined"):
                done[s.topic_id] = s.accept_status
    return [{"id": tp.id, "category": c.name, "category_key": c.key, "prompt_text": tp.prompt_text, "point_value": tp.point_value,
             "mode": tp.mode, "required": tp.required, "status": done.get(tp.id)} for tp, c in rows]

@app.get("/api/brands")
def brands(category_key: str, db: Session = Depends(get_db)):
    c = db.scalar(select(Category).where(Category.key == category_key))
    if not c:
        return []
    return [b.canonical_name for b in db.scalars(select(Brand).where(Brand.category_id == c.id).order_by(Brand.canonical_name))]

class SubmitIn(BaseModel):
    topic_id: int
    link_url: str = ""
    own_brand_text: str | None = None
    intent: str | None = None
    declined: bool = False                 # 前提のお題：内容を提出しない（ポイントなし。件数だけ数える）
    own_category: str | None = None        # 系統B
    outcome: str | None = None             # 系統B：bought / considering / not_bought
    chosen_text: str | None = None
    appeal_tags: list[str] = []
    rejection_tags: list[str] = []
    other_text: str | None = None

@app.post("/api/submissions")
def submit(body: SubmitIn, bg: BackgroundTasks, uid: str = Depends(line_user_id), db: Session = Depends(get_db)):
    p = get_panelist(db, uid)
    if not p:
        raise HTTPException(400, "先に登録してください")
    topic = db.get(Topic, body.topic_id)
    if not topic:
        raise HTTPException(404, "お題がありません")
    if topic.mode != "own":   # 系統B は同じお題に何件でも出せる
        exists = db.scalar(select(Submission).where(Submission.panelist_id == p.id, Submission.topic_id == topic.id,
                                                     Submission.accept_status.in_(["accepted", "reference", "pending", "declined"])))
        if exists:
            raise HTTPException(409, "このお題はすでに提出済みです")
    if body.declined and topic.mode == "premise":
        s = Submission(panelist_id=p.id, topic_id=topic.id, link_url="", link_type="none", fetch_status="skipped",
                       match_status="declined", accept_status="declined", reject_reason="本人が提出しないことを選択")
        db.add(s); db.commit()
        return {"id": s.id, "status": "declined"}
    link_type, reason = classify_link(body.link_url)
    if reason:
        raise HTTPException(422, reason)
    if topic.mode == "own":
        if body.outcome not in ("bought", "considering", "not_bought"):
            raise HTTPException(422, "相談した結果を選んでください")
        if not (body.own_category or "").strip():
            raise HTTPException(422, "カテゴリを選んでください")
    s = Submission(panelist_id=p.id, topic_id=topic.id, link_url=body.link_url.strip(), link_type=link_type,
                   own_brand_text=(body.own_brand_text or "").strip() or None, intent=body.intent,
                   own_category=(body.own_category or "").strip()[:60] or None)
    db.add(s); db.flush()
    if topic.mode == "own":
        db.add(OwnAnswer(submission_id=s.id, outcome=body.outcome, chosen_text=(body.chosen_text or "").strip() or None,
                         appeal_tags=",".join(body.appeal_tags), rejection_tags=",".join(body.rejection_tags), other_text=(body.other_text or "").strip() or None))
    db.commit()
    bg.add_task(process_submission, s.id)
    return {"id": s.id, "status": "pending"}

@app.get("/api/submissions/{sid}")
def submission(sid: int, uid: str = Depends(line_user_id), db: Session = Depends(get_db)):
    p = get_panelist(db, uid)
    s = db.get(Submission, sid)
    if not s or not p or s.panelist_id != p.id:
        raise HTTPException(404)
    out = {"id": s.id, "fetch_status": s.fetch_status, "match_status": s.match_status, "accept_status": s.accept_status, "reject_reason": s.reject_reason}
    if s.accept_status in ("accepted", "reference"):
        r = db.scalar(select(Response).where(Response.submission_id == s.id))
        ms = db.execute(select(Mention, Brand).join(Brand, Mention.brand_id == Brand.id).where(Mention.response_id == r.id, Mention.mention_type == "recommended").order_by(Mention.rank)).all() if r else []
        out["brands"] = [(m.product_text or b.canonical_name) for m, b in ms]
        out["mentions"] = [{"id": m.id, "rank": m.rank, "label": (m.product_text or b.canonical_name)} for m, b in ms]
        out["has_reaction"] = db.scalar(select(SubmissionReaction.id).where(SubmissionReaction.submission_id == s.id)) is not None
    return out

class ReactionIn(BaseModel):
    picked_mention_id: int | None = None
    picked_none: bool = False
    will_refer: str | None = None

@app.post("/api/submissions/{sid}/reaction")
def reaction(sid: int, body: ReactionIn, uid: str = Depends(line_user_id), db: Session = Depends(get_db)):
    p = get_panelist(db, uid)
    s = db.get(Submission, sid)
    if not s or not p or s.panelist_id != p.id:
        raise HTTPException(404)
    if body.will_refer not in ("yes", "no", "unknown"):
        raise HTTPException(422, "参考にするかを選んでください")
    db.query(SubmissionReaction).filter_by(submission_id=s.id).delete()
    db.add(SubmissionReaction(submission_id=s.id, picked_mention_id=body.picked_mention_id, picked_none=body.picked_none or body.picked_mention_id is None, will_refer=body.will_refer))
    db.commit()
    return {"ok": True}

# ---------- 運営用 ----------
def admin(token: str):
    if token != ADMIN_TOKEN:
        raise HTTPException(403)

@app.get("/api/admin/summary")
def admin_summary(token: str, db: Session = Depends(get_db)):
    admin(token)
    by = db.execute(select(Submission.accept_status, Submission.match_status, func.count()).group_by(Submission.accept_status, Submission.match_status)).all()
    return {"panelists": db.scalar(select(func.count(Panelist.id))), "submissions": [{"accept": a, "match": m, "n": n} for a, m, n in by]}

@app.get("/api/admin/completions")
def admin_completions(token: str, db: Session = Depends(get_db)):
    """完了コード一覧（クラウドワークスの承認に使う）。コード、必須お題の完了数、系統Bの件数。"""
    admin(token)
    t = now()
    req = db.scalars(select(Topic.id).where(Topic.required.is_(True), Topic.opens_at <= t, (Topic.closes_at.is_(None)) | (Topic.closes_at > t))).all()
    out = []
    for p in db.scalars(select(Panelist).order_by(Panelist.id)):
        subs = db.scalars(select(Submission).where(Submission.panelist_id == p.id)).all()
        done = {s.topic_id for s in subs if s.accept_status in ("accepted", "reference", "declined")}
        own = sum(1 for s in subs if s.own_category and s.accept_status in ("accepted", "reference"))
        declined = sum(1 for s in subs if s.accept_status == "declined")
        out.append({"code": p.completion_code, "panelist_id": p.id, "required_done": sum(1 for r in req if r in done), "required_total": len(req),
                    "own_submissions": own, "declined": declined, "registered_at": p.consented_at.isoformat()})
    return out

@app.get("/api/admin/unmapped")
def admin_unmapped(token: str, db: Session = Depends(get_db)):
    """辞書に当たらなかった現使用ブランド（週1回の辞書更新用）"""
    admin(token)
    rows = db.execute(select(OwnBrand.raw_text, Category.key).join(Submission, OwnBrand.submission_id == Submission.id)
                      .join(Topic, Submission.topic_id == Topic.id).join(Category, Topic.category_id == Category.id)
                      .where(OwnBrand.brand_id.is_(None))).all()
    return [{"category": c, "text": t} for t, c in rows]

@app.get("/api/admin/pending")
def admin_pending(token: str, version: str = "a1", limit: int = 50, db: Session = Depends(get_db)):
    """注釈がまだ付いていない受付済みの提出を、会話（伏字済み）と現在の読み取り結果つきで返す。"""
    admin(token)
    done = {sid for (sid,) in db.execute(select(Annotation.submission_id).where(Annotation.version == version).distinct())}
    out = []
    rows = db.execute(select(Submission, Topic, Category).join(Topic, Submission.topic_id == Topic.id).join(Category, Topic.category_id == Category.id)
                      .where(Submission.accept_status.in_(["accepted", "reference"])).order_by(Submission.id)).all()
    for s, t, c in rows:
        if s.id in done:
            continue
        r = db.scalar(select(Response).where(Response.submission_id == s.id))
        ms = db.execute(select(Mention, Brand).join(Brand, Mention.brand_id == Brand.id).where(Mention.response_id == r.id).order_by(Mention.rank)).all() if r else []
        out.append({"submission_id": s.id, "category": c.key, "category_name": c.name, "own_brand_text": s.own_brand_text, "answer_note": r.answer_note if r else None,
                    "turns": [{"idx": tn.idx, "role": tn.role, "kind": tn.kind, "text": tn.redacted_text} for tn in db.scalars(select(ConversationTurn).where(ConversationTurn.submission_id == s.id).order_by(ConversationTurn.idx))],
                    "mentions": [{"rank": m.rank, "brand": b.canonical_name, "type": m.mention_type, "product": m.product_text} for m, b in ms]})
        if len(out) >= limit:
            break
    return {"version": version, "remaining": len(rows) - len(done), "items": out}

class AnnotationIn(BaseModel):
    submission_id: int
    kind: str
    key: str = ""
    value: str

class AnnotationsIn(BaseModel):
    version: str = "a1"
    items: list[AnnotationIn]
    done_submission_ids: list[int] = []   # 注釈が0件でも「処理済み」と記録する提出

@app.post("/api/admin/annotations")
def admin_annotations(body: AnnotationsIn, token: str, db: Session = Depends(get_db)):
    """注釈を書き戻す。同じ提出・同じ版の注釈は置き換える。"""
    admin(token)
    ids = {i.submission_id for i in body.items} | set(body.done_submission_ids)
    for sid in ids:
        if not db.get(Submission, sid):
            raise HTTPException(404, f"submission {sid} がありません")
        db.query(Annotation).filter_by(submission_id=sid, version=body.version).delete()
        db.add(Annotation(submission_id=sid, kind="done", key="", value="", version=body.version))
    for i in body.items:
        if i.kind not in ("condition", "criterion", "product", "new_brand", "personalization"):
            raise HTTPException(422, f"kind が不正です: {i.kind}")
        db.add(Annotation(submission_id=i.submission_id, kind=i.kind, key=i.key[:60], value=i.value, version=body.version))
    db.commit()
    return {"ok": True, "submissions": len(ids), "annotations": len(body.items)}

@app.get("/api/admin/export/{name}")
def admin_export(name: str, token: str, scope: str = "main", db: Session = Depends(get_db)):
    """analyze.py / build_dashboard_data.py が読む形式の CSV。scope=main は accepted のみ、wide は reference も含む。"""
    admin(token)
    import csv, io
    statuses = ["accepted"] if scope == "main" else ["accepted", "reference"]
    buf = io.StringIO(); w = csv.writer(buf)
    subs = db.execute(select(Submission, Topic, Category).join(Topic, Submission.topic_id == Topic.id).join(Category, Topic.category_id == Category.id)
                      .where(Submission.accept_status.in_(statuses)).order_by(Submission.panelist_id, Submission.id)).all()
    latest = {}
    for s, t, c in subs:  # カテゴリごとに最新1件
        latest[(s.panelist_id, c.key)] = (s, t, c)
    def attr_at(pid, when):
        return db.scalar(select(PanelistAttribute).where(PanelistAttribute.panelist_id == pid, PanelistAttribute.valid_from <= when,
                                                        (PanelistAttribute.valid_to.is_(None)) | (PanelistAttribute.valid_to > when)).order_by(PanelistAttribute.id.desc()))
    def age_band(y):
        if not y: return ""
        a = dt.date.today().year - y
        return "18-34" if a < 35 else "35-49" if a < 50 else "50+"
    if name == "participants":
        w.writerow(["person_id", "gender", "age_band", "plan", "memory", "device"])
        seen = set()
        for (pid, _), (s, t, c) in latest.items():
            if pid in seen: continue
            seen.add(pid); a = attr_at(pid, s.submitted_at)
            w.writerow([pid, {"female": "F", "male": "M"}.get(a.gender, "X") if a else "", age_band(a.birth_year) if a else "", a.ai_plan or "" if a else "", a.memory_setting or "" if a else "", a.device or "" if a else ""])
    elif name == "responses":
        w.writerow(["person_id", "category", "model", "used_search", "device", "match_status", "extract_status", "personalization"])
        for (pid, ck), (s, t, c) in latest.items():
            r = db.scalar(select(Response).where(Response.submission_id == s.id)); a = attr_at(pid, s.submitted_at)
            ps = db.scalar(select(PersonalizationSignal).where(PersonalizationSignal.response_id == r.id)) if r else None
            w.writerow([pid, ck, "", int(r.used_search) if r else "", a.device if a else "", s.match_status, r.extract_status if r else "", ps.level if ps else ""])
    elif name == "mentions":
        w.writerow(["person_id", "category", "brand", "rank"])
        for (pid, ck), (s, t, c) in latest.items():
            r = db.scalar(select(Response).where(Response.submission_id == s.id))
            for m, b in db.execute(select(Mention, Brand).join(Brand, Mention.brand_id == Brand.id).where(Mention.response_id == r.id, Mention.mention_type == "recommended", Mention.is_primary.is_not(False)).order_by(Mention.rank)):
                w.writerow([pid, ck, b.canonical_name, m.rank])
    elif name == "products":   # 商品単位（同じブランドの別商品も1行ずつ）
        w.writerow(["person_id", "category", "rank", "brand", "product", "is_numbered"])
        for (pid, ck), (s, t, c) in latest.items():
            r = db.scalar(select(Response).where(Response.submission_id == s.id))
            for m, b in db.execute(select(Mention, Brand).join(Brand, Mention.brand_id == Brand.id).where(Mention.response_id == r.id, Mention.mention_type == "recommended").order_by(Mention.rank)):
                fix = db.scalar(select(Annotation.value).where(Annotation.submission_id == s.id, Annotation.kind == "product", Annotation.key == str(m.rank)).order_by(Annotation.id.desc()))
                w.writerow([pid, ck, m.rank, b.canonical_name, fix or m.product_text or "", int(m.is_numbered)])
    elif name == "own_brand":
        w.writerow(["person_id", "category", "brand", "raw_text"])
        for (pid, ck), (s, t, c) in latest.items():
            for ob in db.scalars(select(OwnBrand).where(OwnBrand.submission_id == s.id)):
                b = db.get(Brand, ob.brand_id) if ob.brand_id else None
                w.writerow([pid, ck, b.canonical_name if b else "", ob.raw_text])
    elif name == "turns":      # 結果に至るまでの会話（伏字済み）
        w.writerow(["person_id", "category", "submission_id", "idx", "role", "kind", "n_sources", "text"])
        for (pid, ck), (s, t, c) in latest.items():
            for tn in db.scalars(select(ConversationTurn).where(ConversationTurn.submission_id == s.id).order_by(ConversationTurn.idx)):
                w.writerow([pid, ck, s.id, tn.idx, tn.role, tn.kind, tn.n_sources, tn.redacted_text])
    elif name == "sources":    # 何をもとに推薦したか（参照元）
        w.writerow(["person_id", "category", "submission_id", "turn_idx", "is_answer_turn", "domain", "url", "title"])
        for (pid, ck), (s, t, c) in latest.items():
            for sc in db.scalars(select(Source).where(Source.submission_id == s.id).order_by(Source.turn_idx, Source.id)):
                w.writerow([pid, ck, s.id, sc.turn_idx, int(sc.is_answer_turn), sc.domain, sc.url, sc.title or ""])
    elif name == "conditions":   # ヒアリングで分かった条件（何を基準に選んだか）
        w.writerow(["person_id", "category", "submission_id", "key", "value"])
        for (pid, ck), (s, t, c) in latest.items():
            for an in db.scalars(select(Annotation).where(Annotation.submission_id == s.id, Annotation.kind == "condition").order_by(Annotation.id)):
                w.writerow([pid, ck, s.id, an.key, an.value])
    elif name == "reactions":    # 提出直後の反応
        w.writerow(["person_id", "category", "submission_id", "picked_rank", "picked_label", "picked_none", "will_refer"])
        for (pid, ck), (s, t, c) in latest.items():
            rx = db.scalar(select(SubmissionReaction).where(SubmissionReaction.submission_id == s.id))
            if not rx: continue
            m = db.get(Mention, rx.picked_mention_id) if rx.picked_mention_id else None
            b = db.get(Brand, m.brand_id) if m else None
            w.writerow([pid, ck, s.id, m.rank if m else "", (m.product_text or b.canonical_name) if m else "", int(rx.picked_none), rx.will_refer or ""])
    elif name == "own_answers":  # 系統B の4問
        w.writerow(["person_id", "submission_id", "own_category", "outcome", "chosen_text", "appeal_tags", "rejection_tags", "other_text", "accept_status"])
        for s in db.scalars(select(Submission).where(Submission.own_category.is_not(None)).order_by(Submission.id)):
            oa = db.scalar(select(OwnAnswer).where(OwnAnswer.submission_id == s.id))
            w.writerow([s.panelist_id, s.id, s.own_category, oa.outcome if oa else "", oa.chosen_text if oa else "", oa.appeal_tags if oa else "", oa.rejection_tags if oa else "", oa.other_text if oa else "", s.accept_status])
    elif name == "premises":     # 前提のお題の回答（伏字済み）と、提出しなかった人
        w.writerow(["person_id", "submission_id", "accept_status", "text"])
        for s, t, c in db.execute(select(Submission, Topic, Category).join(Topic, Submission.topic_id == Topic.id).join(Category, Topic.category_id == Category.id).where(Category.key == "premise").order_by(Submission.id)):
            ans = db.scalar(select(ConversationTurn.redacted_text).where(ConversationTurn.submission_id == s.id, ConversationTurn.kind == "answer"))
            w.writerow([s.panelist_id, s.id, s.accept_status, ans or ""])
    elif name == "criteria":     # AI が示した選定基準（どんな基準で選んだか）
        w.writerow(["person_id", "category", "submission_id", "order", "criterion"])
        for (pid, ck), (s, t, c) in latest.items():
            for an in db.scalars(select(Annotation).where(Annotation.submission_id == s.id, Annotation.kind == "criterion").order_by(Annotation.id)):
                w.writerow([pid, ck, s.id, an.key, an.value])
    elif name == "new_brands":   # 辞書に無かったブランド（辞書更新の材料）
        w.writerow(["category", "submission_id", "rank", "brand"])
        for (pid, ck), (s, t, c) in latest.items():
            for an in db.scalars(select(Annotation).where(Annotation.submission_id == s.id, Annotation.kind == "new_brand").order_by(Annotation.id)):
                w.writerow([ck, s.id, an.key, an.value])
    else:
        raise HTTPException(404)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}.csv"})

@app.get("/")
def index():
    return FileResponse("liff/index.html")
app.mount("/static", StaticFiles(directory="liff"), name="static")
