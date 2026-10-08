"""受付済みで、会話の全発言がまだ保存されていない提出を読み直す（抽出ルールや辞書を直したあとの作り直しにも使う）。
実行: python -m server.backfill [--all]"""
import sys
from sqlalchemy import select
from .db import SessionLocal, init_db, Submission, ConversationTurn
from .process import process_submission

def run(all_=False):
    init_db()
    db = SessionLocal()
    ids = []
    for s in db.scalars(select(Submission).where(Submission.accept_status.in_(["accepted", "reference"]))):
        has = db.scalar(select(ConversationTurn.id).where(ConversationTurn.submission_id == s.id).limit(1))
        if all_ or not has:
            ids.append(s.id)
    db.close()
    for i in ids:
        process_submission(i, reprocess=True)
    print(f"backfill: {len(ids)} submissions", flush=True)

if __name__ == "__main__":
    run("--all" in sys.argv)
