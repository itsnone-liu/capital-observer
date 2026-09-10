"""Storage layer: core tables per docs/TECHNICAL_DESIGN_V1.md sec.6.

SQLAlchemy 2.0 declarative; DATABASE_URL selects backend
(dev: sqlite:///data/capobs.db, prod: postgresql+psycopg2://...).

Principles encoded here:
- append-only history with revision chains (fact_observation.supersedes_id)
- publication time integrity: published_at NULL allowed, never fabricated
- two replay modes: published_at (public-info replay) + ingested_at (real-run replay)
- estimates carry scenario/range_type/assumptions — never naked numbers
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (Boolean, CheckConstraint, DateTime, Float, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint, JSON)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now():
    # naive UTC everywhere: SQLite drops tzinfo; keep single convention
    return dt.datetime.utcnow()


class SourceRegistry(Base):
    """sec.3 capability matrix as a live registry."""
    __tablename__ = "source_registry"
    __table_args__ = (CheckConstraint("status in ('VERIFIED','PARTIAL','UNAVAILABLE','UNTESTED')", name='ck_src_status'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), unique=True)      # entry id
    upstream_id: Mapped[str] = mapped_column(String(128))                # original upstream
    dataset: Mapped[str] = mapped_column(String(128))
    license_note: Mapped[str] = mapped_column(Text, default="")          # permission terms
    history_range: Mapped[str] = mapped_column(String(64), default="")   # e.g. 2010-01~
    frequency: Mapped[str] = mapped_column(String(32), default="")
    publication_delay: Mapped[str] = mapped_column(String(64), default="")
    unit_note: Mapped[str] = mapped_column(Text, default="")
    revision_policy: Mapped[str] = mapped_column(String(128), default="")
    backup_source_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="UNTESTED")
    last_tested_at: Mapped[dt.datetime | None] = mapped_column(DateTime())
    notes: Mapped[str] = mapped_column(Text, default="")


class RawArtifact(Base):
    """sec.4 raw artifact: immutable evidence, hash-addressed."""
    __tablename__ = "raw_artifact"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(160), unique=True)   # dataset:name
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime())
    url: Mapped[str] = mapped_column(Text)                               # keys stripped
    upstream_id: Mapped[str] = mapped_column(String(128))
    content_type: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(32), default="")
    bytes: Mapped[int] = mapped_column(Integer, default=0)


class Entity(Base):
    """sec.5 institution/subject."""
    __tablename__ = "entity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_key: Mapped[str] = mapped_column(String(128), unique=True)    # normalized
    name_raw: Mapped[str] = mapped_column(String(256))
    name_display: Mapped[str] = mapped_column(String(256))
    entity_type: Mapped[str] = mapped_column(String(32))                 # institution/policy_actor/person
    mapping_evidence: Mapped[str] = mapped_column(Text, default="")


class Vehicle(Base):
    """sec.5 fund portfolio / ETF."""
    __tablename__ = "vehicle"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vehicle_key: Mapped[str] = mapped_column(String(64), unique=True)    # fund code / composite
    name: Mapped[str] = mapped_column(String(256))
    vehicle_type: Mapped[str] = mapped_column(String(32))                # etf/open_fund/...
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("entity.id"))
    portfolio_group: Mapped[str] = mapped_column(String(64), default="") # A/C share-class merge group


class ShareClass(Base):
    __tablename__ = "share_class"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle.id"))
    share_code: Mapped[str] = mapped_column(String(32))                  # e.g. 510300 / 510301
    is_representative: Mapped[bool] = mapped_column(Boolean, default=False)  # NAV representative series
    UniqueConstraint("vehicle_id", "share_code", name="uq_shareclass")


class Asset(Base):
    __tablename__ = "asset"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_key: Mapped[str] = mapped_column(String(64), unique=True)      # 600000 / 000300 / 801080
    asset_type: Mapped[str] = mapped_column(String(16))                  # stock/index/fund/industry
    name: Mapped[str] = mapped_column(String(128))
    exchange: Mapped[str] = mapped_column(String(16), default="")


class AssetMembership(Base):
    """sec.5 industry/style membership with effective dating."""
    __tablename__ = "asset_membership"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    group_asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)  # industry/style index
    weight: Mapped[float | None] = mapped_column(Float)                  # None = unweighted member
    classification_version: Mapped[str] = mapped_column(String(32), default="sw_l1_v1")
    effective_from: Mapped[str] = mapped_column(String(10))              # YYYY-MM-DD
    effective_to: Mapped[str | None] = mapped_column(String(10), default=None)
    UniqueConstraint("asset_id", "group_asset_id", "classification_version", "effective_from",
                    name="uq_membership")


class FactObservation(Base):
    """sec.6 fact table: append-only, revision-chained."""
    __tablename__ = "fact_observation"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    artifact_id: Mapped[str] = mapped_column(String(160), index=True)
    subject_key: Mapped[str] = mapped_column(String(64), index=True)     # vehicle/asset/market key
    asset_key: Mapped[str | None] = mapped_column(String(64), index=True)
    metric: Mapped[str] = mapped_column(String(64), index=True)          # close/nav/share/margin_balance...
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    effective_at: Mapped[str] = mapped_column(String(32), index=True)    # date(+-precision) or datetime
    published_at: Mapped[str | None] = mapped_column(String(32), index=True)  # NULL allowed & honest
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime(), default=_now)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey("fact_observation.id"))
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("fact_observation.id"))
    quality_status: Mapped[str] = mapped_column(String(16), default="valid")
    __table_args__ = (
        Index("ix_fact_lookup", "subject_key", "metric", "effective_at"),
        Index("ix_fact_pub", "published_at"),
        CheckConstraint("quality_status in ('valid','degraded','stale','unidentifiable','failed')",
                        name="ck_fact_quality"),
    )


class PositionDisclosure(Base):
    """sec.6 holdings disclosure with explicit scope."""
    __tablename__ = "position_disclosure"
    __table_args__ = (CheckConstraint("disclosure_scope in ('top_holdings','full_portfolio','threshold_list')", name='ck_pdisc_scope'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    artifact_id: Mapped[str] = mapped_column(String(160))
    vehicle_key: Mapped[str] = mapped_column(String(64), index=True)
    asset_key: Mapped[str] = mapped_column(String(64), index=True)
    report_period: Mapped[str] = mapped_column(String(10))               # 2025Q1 / 2024A
    quantity: Mapped[float | None] = mapped_column(Float)                # company-action-adjusted shares
    market_value: Mapped[float | None] = mapped_column(Float)
    weight: Mapped[float | None] = mapped_column(Float)
    disclosure_scope: Mapped[str] = mapped_column(String(24))
    company_action_basis: Mapped[str] = mapped_column(String(128), default="")
    published_at: Mapped[str | None] = mapped_column(String(32))
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime(), default=_now)
    UniqueConstraint("vehicle_key", "asset_key", "report_period", "disclosure_scope",
                    name="uq_position")


class InputSnapshot(Base):
    __tablename__ = "input_snapshot"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_key: Mapped[str] = mapped_column(String(64), unique=True)   # hash of member revision ids
    as_of: Mapped[str] = mapped_column(String(10))
    coverage: Mapped[dict] = mapped_column(JSON)                         # per-dataset coverage stats
    classification_version: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(), default=_now)


class ModelRun(Base):
    __tablename__ = "model_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_key: Mapped[str] = mapped_column(String(64), unique=True)
    model_name: Mapped[str] = mapped_column(String(64))                  # nowcast_weekly / diffusion / cost
    code_commit: Mapped[str] = mapped_column(String(40), default="")
    dep_lock_hash: Mapped[str] = mapped_column(String(64), default="")
    input_snapshot_id: Mapped[int] = mapped_column(ForeignKey("input_snapshot.id"))
    params: Mapped[dict] = mapped_column(JSON)
    seed: Mapped[int | None] = mapped_column(Integer)
    model_version: Mapped[str] = mapped_column(String(32), default="")
    training_window: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(), default=_now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime())


class ScenarioDefinition(Base):
    __tablename__ = "scenario_definition"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(16))                 # L1/L2/L3
    version: Mapped[int] = mapped_column(String(16))
    config: Mapped[dict] = mapped_column(JSON)                           # params/evidence/invalidation
    applies_to: Mapped[str] = mapped_column(String(64), default="")
    UniqueConstraint("scenario_id", "version", name="uq_scenario_version")


class Estimate(Base):
    """sec.6 estimate: always scenario-tagged, bounded, assumption-carrying."""
    __tablename__ = "estimate"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("model_run.id"), index=True)
    scenario_id: Mapped[str] = mapped_column(String(16), index=True)
    subject_key: Mapped[str] = mapped_column(String(64), index=True)
    asset_key: Mapped[str | None] = mapped_column(String(64), index=True)
    metric: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    lower: Mapped[float | None] = mapped_column(Float)
    upper: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32))
    target_population: Mapped[str] = mapped_column(String(128), default="")  # who/what the number describes
    coverage: Mapped[str] = mapped_column(Float, default=1.0)            # fraction of population covered
    range_type: Mapped[str] = mapped_column(String(32), default="")
    assumptions: Mapped[dict] = mapped_column(JSON)
    quality: Mapped[str] = mapped_column(String(16), default="")
    sensitivity: Mapped[dict | None] = mapped_column(JSON)
    invalidation_conditions: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        CheckConstraint("range_type in ('','statistical','scenario_envelope','identification_bound')",
                        name="ck_est_range_type"),
    )


class Interpretation(Base):
    __tablename__ = "interpretation"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("model_run.id"), index=True)
    as_of: Mapped[str] = mapped_column(String(10))
    model_provider: Mapped[str] = mapped_column(String(32), default="template")
    model_name: Mapped[str] = mapped_column(String(64), default="rule_template")
    prompt_version: Mapped[str] = mapped_column(String(16), default="")
    content: Mapped[str] = mapped_column(Text)                           # JSON report contract sec.10
    evidence_refs: Mapped[str] = mapped_column(JSON)
    context_refs: Mapped[str] = mapped_column(JSON)
    validation_status: Mapped[str] = mapped_column(String(16), default="pending")


class ValidationResult(Base):
    __tablename__ = "validation_result"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("model_run.id"))
    kind: Mapped[str] = mapped_column(String(32))                        # data/model/scenario/engineering
    target: Mapped[str] = mapped_column(String(128))
    metric: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(), default=_now)


class JobRun(Base):
    """sec.4 job states."""
    __tablename__ = "job_run"
    __table_args__ = (CheckConstraint("status in ('pending','running','succeeded','partial','failed')", name='ck_job_status'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_key: Mapped[str] = mapped_column(String(128), unique=True)       # business key for idempotency
    job_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error_kind: Mapped[str] = mapped_column(String(32), default="")
    retries: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[str] = mapped_column(Text, default="")
    data_range: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime())
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime())
    lock_token: Mapped[str] = mapped_column(String(64), default="")      # run lock
    notes: Mapped[str] = mapped_column(Text, default="")


def init_db(url: str) -> None:
    from sqlalchemy import create_engine
    eng = create_engine(url, future=True)
    Base.metadata.create_all(eng)
    return eng
