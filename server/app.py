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
from pipeline.fetch import classify_link, normalize_link
import json as _json
GENRES = _json.load(open("pipeline/genres.json", encoding="utf-8"))
GENRE_INDEX = {it["key"]: {**it, "group": g["name"]} for g in GENRES["groups"] for it in g["items"]}

DEV_MODE = os.environ.get("DEV_MODE", "1") == "1"
LINE_CHANNEL_ID = os.environ.get("LINE_CHANNEL_ID")
LIFF_ID = os.environ.get("LIFF_ID", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin")
CONSENT_VERSION = "v1"

app = FastAPI(title="ai-panel")
init_db()

@app.on_event("startup")
def _resume_on_startup():
    import threading
    from .backfill import resume_pending
    threading.Thread(target=resume_pending, args=(1,), daemon=True).start()

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
            "mentions": db.scalar(select(func.count(Mention.id)).where(Mention.mention_type == "recommended", Mention.stage != "first")),
            "annotated": db.scalar(select(func.count(Annotation.id)).where(Annotation.kind == "done")),
            "reactions": db.scalar(select(func.count(SubmissionReaction.id))), "own": db.scalar(select(func.count(OwnAnswer.id))),
            "declined": db.scalar(select(func.count(Submission.id)).where(Submission.accept_status == "declined"))}

# ---------- 画面用 ----------
@app.get("/api/config")
def config():
    return {"liffId": LIFF_ID, "devMode": DEV_MODE, "consentVersion": CONSENT_VERSION, "build": BUILD}

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
    shopping_ai_freq: str | None = None
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
    done_ids = {s.topic_id for s in subs if s.accept_status in ("accepted", "reference")}
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
    done, last = {}, {}
    if p:
        for s in db.scalars(select(Submission).where(Submission.panelist_id == p.id).order_by(Submission.id)):
            if s.accept_status in ("accepted", "reference", "pending"):   # declined（旧画面の「提出しない」）は未提出として扱う
                done[s.topic_id] = s.accept_status; last[s.topic_id] = s.submitted_at
    out = []
    for tp, c in rows:
        st = done.get(tp.id); can_re = False; next_at = None
        if st in ("accepted", "reference") and tp.resubmit_days:
            next_at = last[tp.id] + dt.timedelta(days=tp.resubmit_days)
            can_re = t >= next_at
        out.append({"id": tp.id, "category": c.name, "category_key": c.key, "prompt_text": tp.prompt_text, "point_value": tp.point_value,
                    "mode": tp.mode, "required": tp.required, "status": st, "resubmit_days": tp.resubmit_days, "can_resubmit": can_re,
                    "last_submitted_at": last[tp.id].isoformat() if tp.id in last else None, "next_resubmit_at": next_at.isoformat() if next_at else None})
    return out

@app.get("/api/genres")
def genres():
    return {"groups": GENRES["groups"], "other_points": GENRES["other_points"]}

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
    own_category: str | None = None        # 系統B：その他のときの自由記述
    genre_key: str | None = None           # 系統B：ジャンル key（genres.json）または "other"
    outcome: str | None = None             # 系統B：bought / considering / not_bought
    chosen_text: str | None = None
    appeal_tags: list[str] = []
    rejection_tags: list[str] = []
    other_text: str | None = None
    knew_before: str | None = None
    appeal_text: str | None = None
    runner_up_text: str | None = None
    rejection_text: str | None = None
    candidates_text: str | None = None
    stall_reason_text: str | None = None
    needed_info_text: str | None = None
    not_buy_reason_text: str | None = None

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
                                                     Submission.accept_status.in_(["accepted", "reference", "pending"])).order_by(Submission.id.desc()))
        if exists:
            if topic.resubmit_days and exists.accept_status in ("accepted", "reference") and now() >= exists.submitted_at + dt.timedelta(days=topic.resubmit_days):
                pass   # 期間が過ぎたので再提出できる（履歴として積み上がる）
            else:
                raise HTTPException(409, "このお題はすでに提出済みです" + (f"。{topic.resubmit_days}日後に更新できます" if topic.resubmit_days else ""))
    if body.declined and topic.mode == "premise":
        s = Submission(panelist_id=p.id, topic_id=topic.id, link_url="", link_type="none", fetch_status="skipped",
                       match_status="declined", accept_status="declined", reject_reason="本人が提出しないことを選択")
        db.add(s); db.commit()
        return {"id": s.id, "status": "declined"}
    link_type, reason = classify_link(body.link_url)
    if reason:
        raise HTTPException(422, reason)
    genre_key, own_label, pts = None, None, None
    if topic.mode == "own":
        if body.outcome not in ("bought", "considering", "not_bought"):
            raise HTTPException(422, "相談した結果を選んでください")
        gk = (body.genre_key or "").strip()
        if gk in GENRE_INDEX:
            genre_key, own_label, pts = gk, GENRE_INDEX[gk]["name"], GENRE_INDEX[gk]["points"]
        elif gk == "other" and (body.own_category or "").strip():
            genre_key, own_label, pts = "other", (body.own_category or "").strip()[:60], GENRES["other_points"]
        else:
            raise HTTPException(422, "ジャンルを選んでください")
    s = Submission(panelist_id=p.id, topic_id=topic.id, link_url=normalize_link(body.link_url), link_type=link_type,
                   own_brand_text=(body.own_brand_text or "").strip() or None, intent=body.intent,
                   own_category=own_label, genre_key=genre_key, point_value=pts)
    db.add(s); db.flush()
    if topic.mode == "own":
        db.add(OwnAnswer(submission_id=s.id, outcome=body.outcome, chosen_text=(body.chosen_text or "").strip() or None,
                         appeal_tags=",".join(body.appeal_tags), rejection_tags=",".join(body.rejection_tags), other_text=(body.other_text or "").strip() or None,
                             knew_before=body.knew_before if body.knew_before in ("considered", "name_only", "unknown") else None,
                             appeal_text=(body.appeal_text or "").strip() or None, runner_up_text=(body.runner_up_text or "").strip() or None, rejection_text=(body.rejection_text or "").strip() or None,
                             candidates_text=(body.candidates_text or "").strip() or None, stall_reason_text=(body.stall_reason_text or "").strip() or None, needed_info_text=(body.needed_info_text or "").strip() or None, not_buy_reason_text=(body.not_buy_reason_text or "").strip() or None))
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
        ms = db.execute(select(Mention, Brand).join(Brand, Mention.brand_id == Brand.id).where(Mention.response_id == r.id, Mention.mention_type == "recommended", Mention.stage != "first").order_by(Mention.rank)).all() if r else []
        out["brands"] = [(m.product_text or b.canonical_name) for m, b in ms]
        out["mentions"] = [{"id": m.id, "rank": m.rank, "label": (m.product_text or b.canonical_name)} for m, b in ms]
        out["intent"] = s.intent
        out["has_reaction"] = db.scalar(select(SubmissionReaction.id).where(SubmissionReaction.submission_id == s.id)) is not None
    return out

class ReactionIn(BaseModel):
    picked_mention_id: int | None = None
    picked_none: bool = False
    will_refer: str | None = None          # 旧設問。現在は聞いていない
    reason_tags: list[str] = []
    reason_text: str | None = None

@app.post("/api/submissions/{sid}/reaction")
def reaction(sid: int, body: ReactionIn, uid: str = Depends(line_user_id), db: Session = Depends(get_db)):
    p = get_panelist(db, uid)
    s = db.get(Submission, sid)
    if not s or not p or s.panelist_id != p.id:
        raise HTTPException(404)
    if body.picked_mention_id is None and not body.picked_none:
        raise HTTPException(422, "気になった商品を選んでください")
    db.query(SubmissionReaction).filter_by(submission_id=s.id).delete()
    db.add(SubmissionReaction(submission_id=s.id, picked_mention_id=body.picked_mention_id, picked_none=body.picked_none or body.picked_mention_id is None,
                              will_refer=body.will_refer, reason_tags=",".join(body.reason_tags) or None, reason_text=(body.reason_text or "").strip() or None))
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

class ImportItem(BaseModel):
    topic_key: str                          # お題のカテゴリ key（toner / credit_card / protein / premise / own）
    link_url: str = ""
    own_brand_text: str | None = None
    intent: str | None = None
    picked_text: str | None = None          # 気になった商品（自由記述）
    will_refer: str | None = None
    genre_key: str | None = None            # own のとき
    own_category: str | None = None
    outcome: str | None = None
    chosen_text: str | None = None
    appeal_tags: list[str] = []
    rejection_tags: list[str] = []
    other_text: str | None = None
    knew_before: str | None = None
    appeal_text: str | None = None
    runner_up_text: str | None = None
    rejection_text: str | None = None
    candidates_text: str | None = None
    stall_reason_text: str | None = None
    needed_info_text: str | None = None
    not_buy_reason_text: str | None = None

class ImportIn(BaseModel):
    source: str = "crowdworks"
    external_id: str                        # 作業ID
    attributes: dict = {}
    items: list[ImportItem] = []

def _process_import(sub_ids: list[int], reactions: dict):
    """取り込んだ提出を順に処理し、自由記述の「気になった商品」を読み取った推薦に結び付ける。"""
    for sid in sub_ids:
        process_submission(sid)
        rx = reactions.get(sid)
        if not rx:
            continue
        db = SessionLocal()
        try:
            s = db.get(Submission, sid)
            if not s or s.accept_status not in ("accepted", "reference"):
                continue
            r = db.scalar(select(Response).where(Response.submission_id == sid))
            ms = db.execute(select(Mention, Brand).join(Brand, Mention.brand_id == Brand.id).where(Mention.response_id == r.id, Mention.mention_type == "recommended", Mention.stage != "first").order_by(Mention.rank)).all() if r else []
            txt = (rx.get("picked_text") or "").strip()
            none = (not txt) or txt in ("なし", "ない", "特になし", "無し")
            picked = None
            if not none:
                from pipeline.extract import compile_brands
                brands = [(b.id, b.canonical_name, b.aliases, b.maker) for _, b in ms]
                for bid, name, pat, _mk in compile_brands(brands):
                    if pat.search(txt):
                        picked = next(m.id for m, b in ms if b.id == bid); break
                if picked is None:
                    for m, b in ms:   # 商品名の一部一致
                        pt = (m.product_text or "")
                        if pt and (txt in pt or pt in txt or any(w and len(w) >= 3 and w in txt for w in pt.split())):
                            picked = m.id; break
            db.query(SubmissionReaction).filter_by(submission_id=sid).delete()
            db.add(SubmissionReaction(submission_id=sid, picked_mention_id=picked, picked_none=none, will_refer=rx.get("will_refer"), picked_text=txt or None))
            db.commit()
        finally:
            db.close()

@app.post("/api/admin/import")
def admin_import(body: ImportIn, token: str, bg: BackgroundTasks, db: Session = Depends(get_db)):
    """クラウドワークスなど、LINE を通さずに集めた回答を取り込む。同じ作業IDの同じお題は二重に入れない。"""
    admin(token)
    ref = f"{'cw' if body.source == 'crowdworks' else body.source}:{body.external_id}"
    h = hashlib.sha256(ref.encode()).hexdigest()
    p = db.scalar(select(Panelist).where(Panelist.line_user_hash == h))
    if not p:
        p = Panelist(line_user_hash=h, line_user_id_enc="", consent_version=CONSENT_VERSION, source=body.source, external_ref=ref)
        db.add(p); db.flush()
        allowed = set(RegisterIn.model_fields) - {"consent"}
        db.add(PanelistAttribute(panelist_id=p.id, **{k: v for k, v in body.attributes.items() if k in allowed}))
    t = now()
    topics = {c.key: tp for tp, c in db.execute(select(Topic, Category).join(Category, Topic.category_id == Category.id)
                                                .where(Topic.opens_at <= t, (Topic.closes_at.is_(None)) | (Topic.closes_at > t)))}
    created, skipped, errors, reactions = [], [], [], {}
    for it in body.items:
        tp = topics.get(it.topic_key)
        if not tp:
            errors.append({"topic": it.topic_key, "error": "お題がありません"}); continue
        link_type, reason = classify_link(it.link_url)
        existing = db.scalars(select(Submission).where(Submission.panelist_id == p.id, Submission.topic_id == tp.id)).all()
        if any(e.link_url == normalize_link(it.link_url) for e in existing) or (tp.mode != "own" and any(e.accept_status in ("accepted", "reference", "pending") for e in existing)):
            skipped.append(it.topic_key); continue
        genre_key, own_label, pts = None, None, None
        if tp.mode == "own":
            gk = (it.genre_key or "").strip()
            if gk in GENRE_INDEX:
                genre_key, own_label, pts = gk, GENRE_INDEX[gk]["name"], GENRE_INDEX[gk]["points"]
            else:
                genre_key, own_label, pts = "other", (it.own_category or "その他").strip()[:60], GENRES["other_points"]
        s = Submission(panelist_id=p.id, topic_id=tp.id, link_url=normalize_link(it.link_url), link_type=link_type,
                       own_brand_text=(it.own_brand_text or "").strip() or None, intent=it.intent, own_category=own_label, genre_key=genre_key, point_value=pts)
        if reason:   # リンクの形が不正：取りに行かずに不受理として記録（承認判断に使う）
            s.fetch_status, s.match_status, s.accept_status, s.reject_reason = "skipped", "fail", "rejected", reason
        db.add(s); db.flush()
        if tp.mode == "own":
            db.add(OwnAnswer(submission_id=s.id, outcome=it.outcome or "considering", chosen_text=(it.chosen_text or "").strip() or None,
                             appeal_tags=",".join(it.appeal_tags), rejection_tags=",".join(it.rejection_tags), other_text=(it.other_text or "").strip() or None,
                             knew_before=it.knew_before if it.knew_before in ("considered", "name_only", "unknown") else None,
                             appeal_text=(it.appeal_text or "").strip() or None, runner_up_text=(it.runner_up_text or "").strip() or None, rejection_text=(it.rejection_text or "").strip() or None,
                             candidates_text=(it.candidates_text or "").strip() or None, stall_reason_text=(it.stall_reason_text or "").strip() or None, needed_info_text=(it.needed_info_text or "").strip() or None, not_buy_reason_text=(it.not_buy_reason_text or "").strip() or None))
        if not reason:
            created.append(s.id)
            if it.picked_text is not None or it.will_refer:
                reactions[s.id] = {"picked_text": it.picked_text, "will_refer": it.will_refer}
    db.commit()
    bg.add_task(_process_import, created, reactions)
    return {"panelist_id": p.id, "queued": len(created), "skipped": skipped, "errors": errors}

@app.post("/api/admin/resume")
def admin_resume(token: str, bg: BackgroundTasks):
    """pending のまま止まった提出を処理し直す（サーバー再起動のあとなど）。"""
    admin(token)
    from .backfill import resume_pending
    bg.add_task(resume_pending, 1)
    return {"ok": True}

@app.get("/api/admin/import_status")
def admin_import_status(token: str, source: str = "crowdworks", db: Session = Depends(get_db)):
    """取り込んだ作業IDごとの結果（承認判断用）。"""
    admin(token)
    out = []
    for p in db.scalars(select(Panelist).where(Panelist.source == source).order_by(Panelist.id)):
        rows = db.execute(select(Submission, Category.key).join(Topic, Submission.topic_id == Topic.id).join(Category, Topic.category_id == Category.id).where(Submission.panelist_id == p.id).order_by(Submission.id)).all()
        out.append({"external_ref": p.external_ref, "panelist_id": p.id,
                    "items": [{"topic": k, "accept_status": s.accept_status, "match_status": s.match_status, "reason": s.reject_reason} for s, k in rows]})
    return out

@app.get("/api/admin/completions")
def admin_completions(token: str, db: Session = Depends(get_db)):
    """完了コード一覧（クラウドワークスの承認に使う）。コード、必須お題の完了数、系統Bの件数。"""
    admin(token)
    t = now()
    req = db.scalars(select(Topic.id).where(Topic.required.is_(True), Topic.opens_at <= t, (Topic.closes_at.is_(None)) | (Topic.closes_at > t))).all()
    out = []
    for p in db.scalars(select(Panelist).order_by(Panelist.id)):
        subs = db.scalars(select(Submission).where(Submission.panelist_id == p.id)).all()
        done = {s.topic_id for s in subs if s.accept_status in ("accepted", "reference")}
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
            for m, b in db.execute(select(Mention, Brand).join(Brand, Mention.brand_id == Brand.id).where(Mention.response_id == r.id, Mention.mention_type == "recommended", Mention.stage != "first", Mention.is_primary.is_not(False)).order_by(Mention.rank)):
                w.writerow([pid, ck, b.canonical_name, m.rank])
    elif name == "first_products":   # 最初の回答（質問される前＝前提だけ）の推薦
        w.writerow(["person_id", "category", "rank", "brand", "product", "n_stages"])
        for (pid, ck), (s, t, c) in latest.items():
            r = db.scalar(select(Response).where(Response.submission_id == s.id))
            if not r: continue
            for m, b in db.execute(select(Mention, Brand).join(Brand, Mention.brand_id == Brand.id).where(Mention.response_id == r.id, Mention.stage == "first").order_by(Mention.rank)):
                w.writerow([pid, ck, m.rank, b.canonical_name, m.product_text or "", r.n_stages or ""])
    elif name == "products":   # 商品単位（同じブランドの別商品も1行ずつ）
        w.writerow(["person_id", "category", "rank", "brand", "product", "is_numbered"])
        for (pid, ck), (s, t, c) in latest.items():
            r = db.scalar(select(Response).where(Response.submission_id == s.id))
            for m, b in db.execute(select(Mention, Brand).join(Brand, Mention.brand_id == Brand.id).where(Mention.response_id == r.id, Mention.mention_type == "recommended", Mention.stage != "first").order_by(Mention.rank)):
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
        w.writerow(["person_id", "category", "submission_id", "picked_rank", "picked_label", "picked_none", "reason_tags", "reason_text"])
        for (pid, ck), (s, t, c) in latest.items():
            rx = db.scalar(select(SubmissionReaction).where(SubmissionReaction.submission_id == s.id))
            if not rx: continue
            m = db.get(Mention, rx.picked_mention_id) if rx.picked_mention_id else None
            b = db.get(Brand, m.brand_id) if m else None
            w.writerow([pid, ck, s.id, m.rank if m else "", (m.product_text or b.canonical_name) if m else "", int(rx.picked_none), rx.reason_tags or "", rx.reason_text or ""])
    elif name == "own_answers":  # 系統B の4問
        w.writerow(["person_id", "submission_id", "genre_key", "own_category", "points", "outcome", "chosen_text", "knew_before", "appeal_text", "runner_up_text", "rejection_text", "candidates_text", "stall_reason_text", "needed_info_text", "not_buy_reason_text", "accept_status"])
        for s in db.scalars(select(Submission).where(Submission.own_category.is_not(None)).order_by(Submission.id)):
            oa = db.scalar(select(OwnAnswer).where(OwnAnswer.submission_id == s.id))
            w.writerow([s.panelist_id, s.id, s.genre_key or "", s.own_category, s.point_value or "", oa.outcome if oa else "", (oa.chosen_text or "") if oa else "",
                        (oa.knew_before or "") if oa else "", (oa.appeal_text or "") if oa else "", (oa.runner_up_text or "") if oa else "", (oa.rejection_text or "") if oa else "",
                        (oa.candidates_text or "") if oa else "", (oa.stall_reason_text or "") if oa else "", (oa.needed_info_text or "") if oa else "", (oa.not_buy_reason_text or "") if oa else "", s.accept_status])
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

_INDEX = open("liff/index.html", encoding="utf-8").read()
BUILD = hashlib.sha256(_INDEX.encode()).hexdigest()[:10]

@app.get("/")
def index():
    # LINE 内のブラウザが古い画面を使い続けないよう、キャッシュさせない。画面には版（BUILD）を埋め込み、
    # 画面側が /api/config の版と違うと気づいたら、自分で読み込み直す
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_INDEX.replace("__BUILD__", BUILD), headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})
app.mount("/static", StaticFiles(directory="liff"), name="static")
