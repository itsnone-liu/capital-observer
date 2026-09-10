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

from ingestion.adapters.sina_index import SinaIndexBarsAdapter  # noqa: E402
from ingestion.adapters.sw_industry import SWIndustryBarsAdapter, SWIndustryListAdapter  # noqa: E402
from ingestion.base import JobRunner  # noqa: E402


def today_key(name: str) -> str:
    return f"{name}:{dt.date.today().isoformat()}"


def build_jobs() -> list[dict]:
    """Job registry: (name, adapter factory, cron-like note, tz=Asia/Shanghai)."""
    return [
        # 17:30 CST after close: index bars + industry bars (same-day facts)
        {"name": "sina.index_daily", "cron": "30 17 * * MON-FRI",
         "factory": lambda: SinaIndexBarsAdapter(start="2023-08-01")},
        {"name": "sw.industry_daily", "cron": "40 17 * * MON-FRI",
         "factory": lambda: SWIndustryBarsAdapter(start="2023-08-01")},
        {"name": "sw.industry_list", "cron": "0 8 * * MON-FRI",
         "factory": SWIndustryListAdapter},
    ]


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
