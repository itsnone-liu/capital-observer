#!/usr/bin/env python3
"""ETF 官方日度份额历史回补（研究用）。

从 earliest 开始逐日拉取 SSE 逐日快照 + SZSE 区间日度（150天分块），
落 fact_observation（幂等 upsert）。用于把宽基 ETF 资金环境事实
回补到 2-3 年，参与历史验证。

用法：
  nohup python3 scripts/backfill_etf_shares.py --from 2024-01-02 \
      > /tmp/etf_backfill.log 2>&1 &
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.adapters.etf_share import SSEETFShareAdapter, SZSEETFShareAdapter  # noqa: E402
from ingestion.base import JobRunner  # noqa: E402
from jobs.backfill import DB_URL, RAW_ROOT  # noqa: E402


def trading_days(start: str, end: str) -> list[str]:
    """工作日近似（不含周末）；SSE 快照接口对非交易日自然无数据。"""
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    days, d = [], d0
    while d <= d1:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", default="2024-01-02")
    p.add_argument("--to", dest="end", default=date.today().isoformat())
    p.add_argument("--throttle", type=float, default=1.6)
    args = p.parse_args()
    runner = JobRunner(DB_URL, RAW_ROOT)

    days = trading_days(args.start, args.end)
    print(f"[backfill] SSE {args.start}..{args.end} 共 {len(days)} 个工作日", flush=True)
    n_ok = n_empty = n_err = 0
    for i, d in enumerate(days):
        try:
            adapter = SSEETFShareAdapter(dates=[d])
            stats = runner.run(adapter, job_key=f"etf_backfill_sse:{d}")
            rows = stats.get("rows_written", stats.get("rows", 0)) if isinstance(stats, dict) else 0
            n_ok += 1
            if not rows:
                n_empty += 1
        except Exception as e:  # noqa: BLE001
            n_err += 1
            print(f"[backfill] {d} ERROR {type(e).__name__}: {e}", flush=True)
        if (i + 1) % 25 == 0:
            print(f"[backfill] SSE 进度 {i+1}/{len(days)} ok={n_ok} empty={n_empty} err={n_err}", flush=True)
        time.sleep(args.throttle)

    print(f"[backfill] SSE done: ok={n_ok} empty={n_empty} err={n_err}", flush=True)

    # SZSE 区间（150天分块，730只/天，xlsx ≤6mo/次）
    d0 = date.fromisoformat(args.start)
    d1 = date.fromisoformat(args.end)
    while d0 <= d1:
        d_end = min(d0 + timedelta(days=150), d1)
        try:
            adapter = SZSEETFShareAdapter(start=d0.isoformat(), end=d_end.isoformat(), retries=3)
            stats = runner.run(adapter, job_key=f"etf_backfill_szse:{d0.isoformat()}")
            rows = stats.get("rows_written", stats.get("rows", 0)) if isinstance(stats, dict) else 0
            print(f"[backfill] SZSE {d0}..{d_end} rows={rows}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[backfill] SZSE {d0}..{d_end} ERROR {type(e).__name__}: {e}", flush=True)
        d0 = d_end + timedelta(days=1)
        time.sleep(3.0)
    print("[backfill] all done", flush=True)


if __name__ == "__main__":
    main()
