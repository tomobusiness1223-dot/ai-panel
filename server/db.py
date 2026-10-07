"""データモデル（仕様書4章）。SQLite で始め、PostgreSQL に移せる範囲の型だけ使う。"""
from __future__ import annotations
import os, datetime as dt
from sqlalchemy import create_engine, String, Integer, Float, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///data/panel.db")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

class Base(DeclarativeBase):
    pass

class Panelist(Base):
    __tablename__ = "panelist"
    id: Mapped[int] = mapped_column(primary_key=True)            # 分析用ID。レポートはこれだけを使う
    line_user_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # 突合用（sha256）
    line_user_id_enc: Mapped[str] = mapped_column(Text)          # 配信用。復号できる暗号化（RAW_KEY 未設定時は平文＝開発用）
    consent_version: Mapped[str] = mapped_column(String(20), default="v1")
    consented_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    status: Mapped[str] = mapped_column(String(20), default="active")
    attributes: Mapped[list["PanelistAttribute"]] = relationship(back_populates="panelist")

class PanelistAttribute(Base):
    __tablename__ = "panelist_attribute"
    id: Mapped[int] = mapped_column(primary_key=True)
    panelist_id: Mapped[int] = mapped_column(ForeignKey("panelist.id"), index=True)
    valid_from: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    valid_to: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(10))
    birth_year: Mapped[int | None] = mapped_column(Integer)
    prefecture: Mapped[str | None] = mapped_column(String(10))
    household: Mapped[str | None] = mapped_column(String(20))
    children: Mapped[str | None] = mapped_column(String(10))
    ai_plan: Mapped[str | None] = mapped_column(String(10))
    memory_setting: Mapped[str | None] = mapped_column(String(10))
    usage_freq: Mapped[str | None] = mapped_column(String(20))
    started_at: Mapped[str | None] = mapped_column(String(20))
    device: Mapped[str | None] = mapped_column(String(10))
    panelist: Mapped[Panelist] = relationship(back_populates="attributes")

class Category(Base):
    __tablename__ = "category"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(50))
    type: Mapped[str] = mapped_column(String(10), default="unknown")   # diverse / consensus / unknown
    status: Mapped[str] = mapped_column(String(10), default="active")

class Brand(Base):
    __tablename__ = "brand"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"), index=True)
    canonical_name: Mapped[str] = mapped_column(String(80))
    maker: Mapped[str | None] = mapped_column(String(80))
    aliases: Mapped[str] = mapped_column(Text, default="")          # 改行区切りの正規表現（大小文字無視）

class Topic(Base):
    __tablename__ = "topic"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"))
    prompt_text: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(String(10), default="v1")
    opens_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    closes_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    point_value: Mapped[int] = mapped_column(Integer, default=30)

class Submission(Base):
    __tablename__ = "submission"
    id: Mapped[int] = mapped_column(primary_key=True)
    panelist_id: Mapped[int] = mapped_column(ForeignKey("panelist.id"), index=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topic.id"), index=True)
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    link_url: Mapped[str] = mapped_column(Text)
    link_type: Mapped[str] = mapped_column(String(10))               # share / s_t / c / other
    fetch_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / ok / invalid / error
    match_status: Mapped[str | None] = mapped_column(String(20))     # ok/typo/turn/followup/fallback/modified/samechat/fail
    accept_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / accepted / reference / rejected
    reject_reason: Mapped[str | None] = mapped_column(Text)
    own_brand_text: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(10))           # yes / no / unknown

class Response(Base):
    __tablename__ = "response"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submission.id"), index=True)
    model_label: Mapped[str | None] = mapped_column(String(50))
    used_search: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_text_ref: Mapped[str] = mapped_column(Text)                  # data/raw/<id>.json（暗号化領域）への参照
    redacted_text: Mapped[str] = mapped_column(Text)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    extract_status: Mapped[str] = mapped_column(String(10), default="ok")  # ok / none
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

class Mention(Base):
    __tablename__ = "mention"
    id: Mapped[int] = mapped_column(primary_key=True)
    response_id: Mapped[int] = mapped_column(ForeignKey("response.id"), index=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"))
    mention_type: Mapped[str] = mapped_column(String(15))            # recommended / compared / negative / unknown
    rank: Mapped[int | None] = mapped_column(Integer)
    is_numbered: Mapped[bool] = mapped_column(Boolean, default=False)
    classifier_version: Mapped[str] = mapped_column(String(20), default="rules-v0")

class OwnBrand(Base):
    __tablename__ = "own_brand"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submission.id"), index=True)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brand.id"))
    raw_text: Mapped[str] = mapped_column(Text)

class PersonalizationSignal(Base):
    __tablename__ = "personalization_signal"
    id: Mapped[int] = mapped_column(primary_key=True)
    response_id: Mapped[int] = mapped_column(ForeignKey("response.id"), index=True)
    level: Mapped[str] = mapped_column(String(10))                   # strong / weak / none
    evidence_text: Mapped[str | None] = mapped_column(Text)
    classifier_version: Mapped[str] = mapped_column(String(20), default="rules-v0")

class PointLedger(Base):
    __tablename__ = "point_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    panelist_id: Mapped[int] = mapped_column(ForeignKey("panelist.id"), index=True)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(50))
    ref_table: Mapped[str | None] = mapped_column(String(30))
    ref_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

# follow_up（購買追跡）はテスト範囲では作らない。仕様書4章の列定義を本番段階で追加する。

def init_db():
    os.makedirs("data/raw", exist_ok=True)
    Base.metadata.create_all(engine)
