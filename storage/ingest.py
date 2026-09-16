"""Fact/disclosure writer: adapter records -> storage tables.

Keeps the sec.4 boundary: adapters NEVER write the estimate store; this
module writes facts only, with revision semantics (append + supersede).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from storage.models import (Asset, AssetMembership, FactObservation,
                            PositionDisclosure)


def _ensure_asset(session: Session, asset_key: str, asset_type: str = "stock") -> Asset:
    a = session.execute(select(Asset).where(Asset.asset_key == asset_key)).scalar_one_or_none()
    if a is None:
        a = Asset(asset_key=asset_key, asset_type=asset_type, name="")
        session.add(a)
        session.flush()
    return a


def _ensure_group_asset(session: Session, group_key: str) -> Asset:
    return _ensure_asset(session, group_key, asset_type="index")


def _existing_fact(session: Session, r: dict):
    ak = r.get("asset_key")
    return session.execute(
        select(FactObservation)
        .where(FactObservation.subject_key == r["subject_key"],
               FactObservation.metric == r["metric"],
               FactObservation.effective_at == r["effective_at"],
               FactObservation.asset_key.is_(None) if ak is None
               else FactObservation.asset_key == ak,
               FactObservation.quality_status != "failed")
        .order_by(FactObservation.id.desc())
    ).scalars().first()


def write_records(session: Session, records: list[dict], fill_only: bool = False) -> dict:
    """fill_only=True: existing facts stay untouched (fallback fills gaps only)."""
    stats = {"fact_inserted": 0, "fact_superseded": 0, "disclosure_inserted": 0,
             "skipped_unchanged": 0, "unknown_kind": 0}
    for r in records:
        kind = r.get("kind", "fact")
        if kind == "fact":
            prev = _existing_fact(session, r)
            if prev is not None:
                if abs((prev.value or 0.0) - float(r["value"])) < 1e-12 or fill_only:
                    # 事实值未变，但首次在本系统真实取得时补 available_at；
                    # published_at 仍保持NULL，不能把抓取时间冒充披露时间。
                    if prev.available_at is None and r.get("available_at"):
                        prev.available_at = r["available_at"]
                        stats.setdefault("availability_stamped", 0)
                        stats["availability_stamped"] += 1
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
                    available_at=r.get("available_at"),
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
                    available_at=r.get("available_at"),
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
        elif kind == "asset_info":
            a = _ensure_asset(session, r["asset_key"], r.get("asset_type", "stock"))
            if r.get("name") and not a.name:
                a.name = r["name"]
                stats.setdefault("asset_named", 0)
                stats["asset_named"] += 1
            else:
                stats["skipped_unchanged"] += 1
        elif kind == "membership":
            _ensure_group_asset(session, r["group_key"])
            _ensure_asset(session, r["asset_key"])
            ga = session.execute(select(Asset).where(Asset.asset_key == r["group_key"])).scalar_one()
            ma = session.execute(select(Asset).where(Asset.asset_key == r["asset_key"])).scalar_one()
            asof = r["effective_from"]
            active = session.execute(
                select(AssetMembership).where(
                    AssetMembership.asset_id == ma.id,
                    AssetMembership.classification_version == r["classification_version"],
                    AssetMembership.effective_to.is_(None))
            ).scalars().all()
            same = next((x for x in active if x.group_asset_id == ga.id), None)
            if same is not None:
                stats["skipped_unchanged"] += 1
                continue
            # 新快照显示换组：关闭同分类下旧开放区间，再开新版本。
            for old in active:
                old.effective_to = asof
                stats.setdefault("membership_closed", 0)
                stats["membership_closed"] += 1
            session.add(AssetMembership(
                asset_id=ma.id, group_asset_id=ga.id, weight=r.get("weight"),
                classification_version=r["classification_version"],
                effective_from=asof))
            stats.setdefault("membership_inserted", 0)
            stats["membership_inserted"] += 1
        else:
            stats["unknown_kind"] += 1
    session.commit()
    return stats
