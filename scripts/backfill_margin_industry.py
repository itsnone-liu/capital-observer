#!/usr/bin/env python3
"""回填 行业级两融聚合（交易所明细×申万L1映射），2025-01-02起。

- 逐交易日抓两所明细 → 按当日映射聚合 → fact入库（幂等：值不变跳过）；
- 原始明细落 data/raw/margin_detail/{date}.csv.gz 供审计与再衍生；
- 休市日两所接口无数据→跳过；单日失败重试一次后记录继续。
用法: backfill_margin_industry.py [--start 2025-01-02] [--end 2026-09-15]
       [--sample 3]  # 只跑最后N个交易日验证
"""
from __future__ import annotations

import argparse
import gzip
import sys
import time
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingestion.adapters.margin_detail import (  # noqa: E402
    fetch_detail, industry_records, membership_map)
from storage.models import FactObservation, JobRun, init_db  # noqa: E402
from storage.ingest import write_records  # noqa: E402

RAW = ROOT / "data" / "raw" / "margin_detail"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-02")
    ap.add_argument("--end", default="2026-09-15")
    ap.add_argument("--sample", type=int, default=0, help="只跑区间内最后N个交易日")
    args = ap.parse_args()

    eng = init_db(f"sqlite:///{ROOT / 'data' / 'capobs.db'}")
    days = [d.date().isoformat() for d in pd.date_range(args.start, args.end, freq="B")]
    if args.sample:
        days = days[-args.sample:]
    RAW.mkdir(parents=True, exist_ok=True)
    total = {"days_ok": 0, "days_empty": 0, "days_failed": []}
    run_tag = pd.Timestamp.now().strftime("%Y%m%dT%H%M%S")
    job = JobRun(job_key=f"backfill.margin_detail_sw_l1:{args.start}:{args.end}:{run_tag}",
                 job_type="backfill", status="running",
                 started_at=pd.Timestamp.now().to_pydatetime(),
                 notes=f"start={args.start} end={args.end} n={len(days)}")
    with Session(eng) as s:
        s.add(job)
        s.commit()
        job_id = job.id
    for d in days:
        raw_path = RAW / f"{d}.csv.gz"
        try:
            if raw_path.exists():
                detail = pd.read_csv(raw_path, dtype={"code": str})
                detail["code"] = detail["code"].str.zfill(6)
            else:
                detail = fetch_detail(d)
                if detail.empty:  # 重试一次（上游偶发抖动）
                    time.sleep(2.0)
                    detail = fetch_detail(d)
            if detail.empty:
                total["days_empty"] += 1
                continue
            with gzip.open(raw_path, "wt", encoding="utf-8") as f:
                detail.to_csv(f, index=False)
            with Session(eng) as s:
                mapping = membership_map(s, d)
                records, diag = industry_records(detail, mapping, d)
                stats = write_records(s, records)
                s.commit()
            total["days_ok"] += 1
            if total["days_ok"] % 20 == 0:
                print(f"{d}: industries={len(records)//2} unknown={diag['unknown_count']}"
                      f" inserted={stats['fact_inserted']}", flush=True)
            time.sleep(1.2)
        except Exception as e:
            total["days_failed"].append(f"{d}:{type(e).__name__}")
            continue
    with Session(eng) as s:
        n = s.query(FactObservation).filter(
            FactObservation.source_id == "exchange.margin_detail_sw_l1").count()
        s.query(JobRun).filter(JobRun.id == job_id).update({
            "status": "succeeded",
            "finished_at": pd.Timestamp.now().to_pydatetime(),
            "notes": f"ok={total['days_ok']} empty={total['days_empty']} "
                     f"failed={total['days_failed'][:10]} rows={n}"})
        s.commit()
    print("RESULT", total, "rows_total=", n)


if __name__ == "__main__":
    main()
