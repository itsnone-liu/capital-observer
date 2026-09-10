"""Single-instance scheduler process (design sec.2/4).

Separate from API workers (API never starts the scheduler). Jobs recorded in
job_run via JobRunner; schedules follow each source's actual publication
time. Run: python3 -m jobs.scheduler [--once]
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_URL = f"sqlite:///{ROOT / 'data' / 'capobs.db'}"
RAW_ROOT = ROOT / "data" / "raw"

from ingestion.adapters.etf import (ETFBarsSinaAdapter, ETFNavAdapter,  # noqa: E402
                                    ETFShareChangeAdapter)
from ingestion.adapters.fund_data import (FundIndustryAllocAdapter,  # noqa: E402
                                          FundNavAdapter)
from ingestion.adapters.margin import (SSEMarginSummaryAdapter,  # noqa: E402
                                       SZSEMarginSummaryAdapter)
from ingestion.adapters.sina_index import SinaIndexBarsAdapter  # noqa: E402
from ingestion.adapters.sw_industry import SWIndustryBarsAdapter, SWIndustryListAdapter  # noqa: E402
from ingestion.base import JobRunner  # noqa: E402
from jobs.backfill import ETF_FIRST  # noqa: E402


def today_key(name: str) -> str:
    return f"{name}:{dt.date.today().isoformat()}"


def build_jobs() -> list[dict]:
    """Job registry: schedules follow each source's actual publication time (Asia/Shanghai)."""
    etf_codes = [s[-6:] for s in ETF_FIRST]
    fund_codes = _fund_sample_codes()
    return [
        # 17:30 after close: index + industry bars (same-day facts, sina/sw)
        {"name": "sina.index_daily", "cron": "30 17 * * MON-FRI",
         "factory": lambda: SinaIndexBarsAdapter(start="2023-08-01")},
        {"name": "sw.industry_daily", "cron": "40 17 * * MON-FRI",
         "factory": lambda: SWIndustryBarsAdapter(start="2023-08-01")},
        {"name": "sw.industry_list", "cron": "0 8 * * MON-FRI",
         "factory": SWIndustryListAdapter},
        # margin: SSE publishes T-day summary ~17:00; SZSE per-day API same evening
        {"name": "margin.sse_daily", "cron": "10 18 * * MON-FRI",
         "factory": SSEMarginSummaryAdapter},
        {"name": "margin.szse_daily", "cron": "20 18 * * MON-FRI",
         "factory": lambda: SZSEMarginSummaryAdapter(dates=[dt.date.today().strftime("%Y%m%d")])},
        # ETF: bars at close, NAV published T evening (~20:00)
        {"name": "etf.bars_daily", "cron": "50 17 * * MON-FRI",
         "factory": lambda: ETFBarsSinaAdapter(symbols=ETF_FIRST, start="20230801")},
        {"name": "etf.nav_daily", "cron": "30 20 * * MON-FRI",
         "factory": lambda: ETFNavAdapter(codes=etf_codes)},
        # quarterly disclosures: fund share change + industry alloc (quarter ends +15d)
        {"name": "etf.share_quarterly", "cron": "0 7 15 1,4,7,10",
         "factory": lambda: ETFShareChangeAdapter(codes=etf_codes)},
        {"name": "fund.nav_daily", "cron": "45 20 * * MON-FRI",
         "factory": lambda: FundNavAdapter(codes=fund_codes)},
        # CSI weights: monthly snapshot (month-end +1d)
        {"name": "csi.weights_monthly", "cron": "0 8 2 * *",
         "factory": lambda: __import__("ingestion.adapters.etf", fromlist=["CSIWeightAdapter"])
         .CSIWeightAdapter(index_codes=["000300", "000905", "000852"])},
    ]


def _fund_sample_codes() -> list[str]:
    """Sample fund codes from the persisted hash-sample (stable across days)."""
    import json
    f = ROOT / "data" / "p0_results" / "fund_sample.json"
    if f.exists():
        return json.loads(f.read_text())["codes"]
    return ["005827", "110022", "000001"]  # fallback: verified trio


def run_all_once(runner: JobRunner) -> None:
    for j in build_jobs():
        name = j["name"]
        print(f"[scheduler] running {name}", flush=True)
        try:
            summary = runner.run(j["factory"](), job_key=today_key(name))
            print(f"[scheduler] {name}: {summary.get('status')} ok={summary.get('ok')} "
                  f"failed={summary.get('failed')} writes={summary.get('writes')}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] {name} CRASHED: {exc}", flush=True)


def main() -> None:
    if "--once" in sys.argv:
        run_all_once(JobRunner(DB_URL, RAW_ROOT))
        return
    from apscheduler.schedulers.blocking import BlockingScheduler  # lazy import
    sched = BlockingScheduler(timezone="Asia/Shanghai")
    runner = JobRunner(DB_URL, RAW_ROOT)
    for j in build_jobs():
        cron = j["cron"].split()
        sched.add_job(runner.run, "cron",
                      args=[j["factory"]()], id=j["name"],
                      name=j["name"],
                      day_of_week=cron[4], hour=int(cron[1]), minute=int(cron[0]),
                      misfire_grace_time=3600, coalesce=True,
                      kwargs={"job_key": today_key(j["name"])})
        print(f"[scheduler] registered {j['name']} @ {j['cron']}")
    print("[scheduler] starting (Ctrl-C to stop)", flush=True)
    sched.start()


if __name__ == "__main__":
    main()
