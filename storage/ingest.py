"""Fact/disclosure writer: adapter records -> storage tables.

Keeps the sec.4 boundary: adapters NEVER write the estimate store; this
module writes facts only, with revision semantics (append + supersede).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from storage.models import FactObservation, PositionDisclosure


def _existing_fact(session: Session, r: dict):
    return session.execute(
        select(FactObservation)
        .where(FactObservation.subject_key == r["subject_key"],
               FactObservation.metric == r["metric"],
               FactObservation.effective_at == r["effective_at"],
               FactObservation.quality_status != "failed")
        .order_by(FactObservation.id.desc())
    ).scalars().first()


def write_records(session: Session, records: list[dict]) -> dict:
    stats = {"fact_inserted": 0, "fact_superseded": 0, "disclosure_inserted": 0,
             "skipped_unchanged": 0, "unknown_kind": 0}
    for r in records:
        kind = r.get("kind", "fact")
        if kind == "fact":
            prev = _existing_fact(session, r)
            if prev is not None:
                if abs((prev.value or 0.0) - float(r["value"])) < 1e-12:
                    stats["skipped_unchanged"] += 1
                    continue
                prev.quality_status = "stale"  # keep history, mark superseded
                prev.supersedes_id = None
                nu = FactObservation(
                    source_id=r["source_id"], artifact_id=r["artifact_id"],
                    subject_key=r["subject_key"], asset_key=r.get("asset_key"),
                    metric=r["metric"], value=float(r["value"]), unit=r["unit"],
                    currency=r.get("currency", "CNY"),
                    effective_at=r["effective_at"], published_at=r.get("published_at"),
                    quality_status=r.get("quality_status", "valid"),
                    supersedes_id=prev.id)
                session.add(nu)
                stats["fact_superseded"] += 1
            else:
                session.add(FactObservation(
                    source_id=r["source_id"], artifact_id=r["artifact_id"],
                    subject_key=r["subject_key"], asset_key=r.get("asset_key"),
                    metric=r["metric"], value=float(r["value"]), unit=r["unit"],
                    currency=r.get("currency", "CNY"),
                    effective_at=r["effective_at"], published_at=r.get("published_at"),
                    quality_status=r.get("quality_status", "valid")))
                stats["fact_inserted"] += 1
        elif kind == "disclosure":
            prev = session.execute(
                select(PositionDisclosure).where(
                    PositionDisclosure.vehicle_key == r["vehicle_key"],
                    PositionDisclosure.asset_key == r["asset_key"],
                    PositionDisclosure.report_period == r["report_period"],
                    PositionDisclosure.disclosure_scope == r["disclosure_scope"])
            ).scalar_one_or_none()
            if prev is not None:
                stats["skipped_unchanged"] += 1
                continue
            session.add(PositionDisclosure(
                source_id=r["source_id"], artifact_id=r["artifact_id"],
                vehicle_key=r["vehicle_key"], asset_key=r["asset_key"],
                report_period=r["report_period"],
                quantity=r.get("quantity"), market_value=r.get("market_value"),
                weight=r.get("weight"), disclosure_scope=r["disclosure_scope"],
                company_action_basis=r.get("company_action_basis", ""),
                published_at=r.get("published_at")))
            stats["disclosure_inserted"] += 1
        else:
            stats["unknown_kind"] += 1
    session.commit()
    return stats
