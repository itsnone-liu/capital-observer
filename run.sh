#!/usr/bin/env bash
# capital-observer operations entrypoint (P5 trial-run mode)
set -e
cd "$(dirname "$0")"
PY=/root/.hermes/hermes-agent/venv/bin/python3

case "${1:-help}" in
  api)        # serve API+dashboard on :8120
    exec $PY -m uvicorn api.app:app --host 0.0.0.0 --port 8120 ;;
  scheduler)  # resident daily scheduler (publication-time crons, Asia/Shanghai)
    exec $PY -m jobs.scheduler ;;
  once)       # run all jobs once (manual backfill / catch-up)
    exec $PY -m jobs.scheduler --once ;;
  fallback)   # baostock index fallback fill (run when sina unreachable)
    exec $PY -c "import sys; sys.path.insert(0,'.')
from ingestion.adapters.baostock_index import BaoStockIndexBarsAdapter
from ingestion.base import JobRunner
from jobs.backfill import DB_URL, RAW_ROOT
import datetime as dt
s = JobRunner(DB_URL, RAW_ROOT).run(BaoStockIndexBarsAdapter(), job_key='fallback:bs:'+dt.date.today().isoformat())
print('fallback:', s.get('status'), s.get('writes'))" ;;
  report)     # regenerate weekly report
    exec $PY models/report.py ;;
  status)
    $PY -c "import sqlite3; con=sqlite3.connect('data/capobs.db')
print('facts:', con.execute('select count(*) from fact_observation').fetchone()[0])
print('jobs today:', con.execute(\"select count(*) from job_run where started_at >= date('now')\").fetchone()[0])" ;;
  *) echo "usage: ./run.sh {api|scheduler|once|fallback|report|status}" ;;
esac
