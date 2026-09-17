#!/usr/bin/env python3
"""一次性保守回补背景事实 available_at（2026-09-17 用户裁定）。

裁定：指数/ETF/申万指数等背景数据用保守可用规则（背景监测比对用途，
不要求精确）。口径分两档：
  Tier A 收盘即知（价格/量/额）：index_close/amount/volume、
       etf_close/amount/volume、sw_close/sw_amount → T日 15:30
  Tier B 官方盘后发布（份额/净值）：etf_total_shares、etf_nav、fund_nav
       → T日 22:00
不在范围（维持NULL，不猜）：margin_fin_*（交易所明细）、sf_*（保留原戳）、
已带戳指标原样不动。

幂等：只更新 available_at IS NULL 的行；重复执行零改动。
默认 dry-run，--apply 才落库；JobRun 留审计记录。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobs.backfill import DB_URL  # noqa: E402
from storage.models import FactObservation, JobRun, init_db  # noqa: E402

METHOD_VERSION = "available-at-background-conservative-v1"

TIER_A = ("index_close", "index_amount", "index_volume",
          "etf_close", "etf_amount", "etf_volume",
          "sw_close", "sw_amount")                     # T日15:30
TIER_B = ("etf_total_shares", "etf_nav", "fund_nav")   # T日22:00
TIERS = {"A": (TIER_A, "T15:30:00"), "B": (TIER_B, "T22:00:00")}


def _pending_count(session: Session, metrics: tuple[str, ...]) -> int:
    return int(session.execute(
        select(func.count()).select_from(FactObservation)
        .where(FactObservation.available_at.is_(None),
               FactObservation.metric.in_(metrics))).scalar() or 0)


def stamp(session: Session, apply: bool) -> dict:
    result = {"method": METHOD_VERSION, "tiers": {"A": "T15:30", "B": "T22:00"},
              "scope_note": "margin_fin_* 不在范围；sf_*/已带戳指标不动",
              "stamped": {}}
    for tier, (metrics, suffix) in TIERS.items():
        if apply:
            res = session.execute(
                update(FactObservation)
                .where(FactObservation.available_at.is_(None),
                       FactObservation.metric.in_(metrics))
                .values(available_at=FactObservation.effective_at.concat(suffix)))
            rows = int(res.rowcount or 0)
        else:
            rows = _pending_count(session, metrics)
        key = "rows" if apply else "would_stamp"
        result["stamped"][tier] = {"metrics": list(metrics), key: rows}
    if apply:
        import uuid
        result["applied_at"] = dt.datetime.utcnow().isoformat(timespec="seconds")
        session.add(JobRun(
            job_key=f"stamp_background_availability:{METHOD_VERSION}:"
                    f"{result['applied_at']}:{uuid.uuid4().hex[:6]}",
            job_type="availability_stamp", status="succeeded",
            notes=json.dumps(result, ensure_ascii=False)))
        session.commit()
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_URL)
    p.add_argument("--apply", action="store_true", help="默认dry-run，仅计数")
    a = p.parse_args()
    engine = init_db(a.db)
    with Session(engine) as s:
        print(json.dumps(stamp(s, apply=a.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
