"""Historical backfill orchestrator (P1): run verified adapters over the
first-batch universe and land facts into data/capobs.db.

Usage: python3 -m jobs.backfill [--quick]   # --quick = small smoke subset
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_URL = f"sqlite:///{ROOT / 'data' / 'capobs.db'}"
RAW_ROOT = ROOT / "data" / "raw"

from ingestion.adapters.etf import (CSIWeightAdapter, ETFBarsSinaAdapter,
                                    ETFNavAdapter, ETFShareChangeAdapter)
from ingestion.adapters.margin import MarginDetailDayAdapter, SSEMarginSummaryAdapter
from ingestion.adapters.sina_index import INDEX_UNIVERSE, SinaIndexBarsAdapter
from ingestion.adapters.sw_industry import SWIndustryBarsAdapter
from ingestion.base import JobRunner

ETF_FIRST = ["sh510300", "sh510050", "sh510500", "sh512100", "sz159915",
             "sh588000", "sh563300", "sh512480", "sh512760", "sh512690",
             "sh515790", "sh512800", "sh512010", "sz159928", "sh515030"]
ETF_CODES = [s[-6:] for s in ETF_FIRST]


def trade_dates_recent(n: int = 10) -> list[str]:
    """Last n weekdays as YYYYMMDD (approx; exchange detail fetch will 404 holidays)."""
    days, d = [], dt.date.today()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime("%Y%m%d"))
        d -= dt.timedelta(days=1)
    return sorted(days)


def main() -> None:
    quick = "--quick" in sys.argv
    runner = JobRunner(DB_URL, RAW_ROOT)
    run = runner.run
    tag = dt.date.today().isoformat()

    def go(name, adapter, key_suffix=""):
        print(f"\n### backfill {name}", flush=True)
        s = run(adapter, job_key=f"backfill:{name}:{tag}{key_suffix}")
        print(f"### {name}: {s.get('status')} ok={s.get('ok')} failed={s.get('failed')} "
              f"writes={s.get('writes', {})} warns={s.get('warnings', [])[:3]}", flush=True)

    # 1. indices (full history from sina, one call each)
    go("index_daily", SinaIndexBarsAdapter(start="2023-08-01"))

    # 2. SW industry bars (31 industries, full history each)
    go("sw_industry_daily", SWIndustryBarsAdapter(start="2023-08-01"))

    # 3. SSE margin summary (range API)
    go("sse_margin", SSEMarginSummaryAdapter(start="20230801"))

    # 4. margin detail: recent N trade days (per-day files, both exchanges)
    n_days = 3 if quick else 10
    go("margin_detail", MarginDetailDayAdapter(dates=trade_dates_recent(n_days)))

    # 5. ETF: bars + NAV + quarterly share
    etfs = ETF_FIRST[:3] if quick else ETF_FIRST
    go("etf_bars", ETFBarsSinaAdapter(symbols=etfs, start="20230801"))
    go("etf_nav", ETFNavAdapter(codes=[s[-6:] for s in etfs]))
    go("etf_share_q", ETFShareChangeAdapter(codes=[s[-6:] for s in etfs]))

    # 6. CSI weights snapshot (monthly archive starts now)
    go("csi_weights", CSIWeightAdapter(index_codes=["000300", "000905", "000852"][:1 if quick else 3]))

    print("\nbackfill complete", flush=True)


if __name__ == "__main__":
    main()
