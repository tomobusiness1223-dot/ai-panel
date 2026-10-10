"""受付済みで、会話の全発言がまだ保存されていない提出を読み直す（抽出ルールや辞書を直したあとの作り直しにも使う）。
実行: python -m server.backfill [--all]"""
import sys
from sqlalchemy import select
from .db import SessionLocal, init_db, Submission, ConversationTurn, Response
from pipeline.extract import EXTRACT_VERSION
from .process import process_submission

def resume_pending(max_age_minutes: int = 2) -> int:
    """処理の途中でサーバーが再起動して pending のまま残った提出を、順に処理し直す。"""
    import datetime as dt
    init_db()
    db = SessionLocal()
    cutoff = dt.datetime.utcnow() - dt.timedelta(minutes=max_age_minutes)
    ids = [s.id for s in db.scalars(select(Submission).where(Submission.accept_status == "pending", Submission.submitted_at < cutoff).order_by(Submission.id))]
    db.close()
    for i in ids:
        process_submission(i)
    print(f"resume_pending: {len(ids)} submissions", flush=True)
    return len(ids)

def run(all_=False):
    init_db()
    db = SessionLocal()
    # 2026-10-10：答える前の共有（early）も受付扱いにした。参考のまま残っている分を直す（取り直し不要）
    for s in db.scalars(select(Submission).where(Submission.match_status == "early", Submission.accept_status == "reference")):
        s.accept_status = "accepted"
    db.commit()
    ids = []
    for s in db.scalars(select(Submission).where(Submission.accept_status.in_(["accepted", "reference"]))):
        has = db.scalar(select(ConversationTurn.id).where(ConversationTurn.submission_id == s.id).limit(1))
        r = db.scalar(select(Response).where(Response.submission_id == s.id))
        if all_ or not has or r is None or r.extract_version != EXTRACT_VERSION:
            ids.append(s.id)
    db.close()
    import time
    for i in ids:
        process_submission(i, reprocess=True)
        time.sleep(1.0)   # 続けて取りに行って混雑時の確認ページを返されないように、少し間をあける
    print(f"backfill: {len(ids)} submissions", flush=True)

if __name__ == "__main__":
    if "--pending" in sys.argv:
        resume_pending(0)
    else:
        run("--all" in sys.argv)
