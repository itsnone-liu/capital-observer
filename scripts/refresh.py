#!/usr/bin/env python3
"""One-command refresh: latest data -> recompute caches -> reload web API.

Triggered by the capital-observer skill when the user asks e.g.
"查看主力情况". Steps (all idempotent, safe to re-run):

 1. EM sector flow snapshot (all boards, today)
 2. EM sector flow history for major-group boards (direct; falls back to
    the r.jina.ai read-proxy when push2his throttles this IP)
 3. Stock-level margin detail for the latest trading day
 4. TDX 880 board-index dailies from local vipdoc
 5. Recompute fund-exposure + ETF-equivalent timeseries cache
 6. Restart uvicorn on :8120 and verify the panels endpoint

Usage: python3 scripts/refresh.py [--skip-history]
"""
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def log(m):
    print(f"[{dt.datetime.now():%H:%M:%S}] {m}", flush=True)


def sh(cmd, timeout=1200):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        log(f"  !! {cmd}\n{r.stdout[-400:]}\n{r.stderr[-400:]}")
    return r.returncode


def main(skip_history=False):
    from ingestion.adapters.em_flow import (EMSectorFlowSnapshotAdapter,
                                            EMSectorFlowHistoryAdapter)
    from ingestion.adapters.margin import MarginDetailDayAdapter
    from ingestion.base import JobRunner
    from jobs.backfill import DB_URL, RAW_ROOT
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    today = dt.date.today().isoformat()
    runner = JobRunner(DB_URL, RAW_ROOT)
    engine = create_engine(DB_URL, connect_args={"timeout": 60})

    # 1. today's board snapshot
    st = runner.run(EMSectorFlowSnapshotAdapter(), job_key=f"em_flow_snap:{today}")
    log(f"snapshot: {st.get('status')} ok={st.get('ok')}")

    # 2. history for major groups (direct first, proxy fallback)
    if not skip_history:
        groups = json.loads((ROOT / "configs" / "major_groups.json").read_text())
        from storage.models import Asset
        from sqlalchemy import select
        with Session(engine) as s:
            names = {n for spec in groups.values() for n in spec["main"]}
            amap = dict(s.execute(select(Asset.asset_key, Asset.name)
                                  .where(Asset.asset_type == "em_board")).all())
            boards = sorted(k for k, nm in amap.items() if nm in names)
        st = runner.run(EMSectorFlowHistoryAdapter(boards, 250),
                        job_key=f"em_flow_hist:refresh:{today}")
        ok = st.get("ok") or 0
        log(f"history direct: {st.get('status')} ok={ok} failed={st.get('failed')}")
        if ok == 0:
            log("direct blocked; retrying through r.jina.ai proxy")
            # self-contained proxy fetch with hard budget: requests' timeout
            # does NOT cover DNS resolution (getaddrinfo can hang forever),
            # and fetch-retries x JobRunner-retries multiply into tens of
            # minutes. Budget = per-attempt checks + thread-level hard stop.
            import threading
            import requests as _rq
            import re as _re
            PROXY_BUDGET = 720  # seconds, hard wall for the whole proxy phase
            _deadline = time.monotonic() + PROXY_BUDGET

            def fetch_via_jina(self, item):
                q = (f"secid=90.{item}&lmt={self.lmt}&klt=101&fields1=f1,f2,f3,f7"
                     "&fields2=f51,f52,f53,f54,f55,f56,f61,f62")
                last = None
                for attempt in range(4):
                    if time.monotonic() > _deadline:
                        raise RuntimeError(f"proxy budget exhausted ({PROXY_BUDGET}s)")
                    try:
                        r = _rq.get("https://r.jina.ai/https://push2his.eastmoney.com"
                                    "/api/qt/stock/fflow/daykline/get?" + q,
                                    headers={"User-Agent": "Mozilla/5.0"},
                                    timeout=(10, 40))  # (connect, read)
                        m = _re.search(r'"klines"\s*:\s*\[(.*?)\]', r.text, _re.S)
                        if not m:
                            raise ValueError(f"no klines ({len(r.text)} chars)")
                        lines = [x.strip().strip('"') for x in m.group(1).split('","')]
                        lines = [x for x in lines if _re.match(r"^\d{4}-\d{2}-\d{2}", x)]
                        if not lines:
                            raise ValueError("empty klines")
                        return {"payload": "\n".join(lines), "url": f"jina:fflow/{item}",
                                "content_type": "text/csv"}
                    except Exception as e:
                        last = e
                        log(f"  proxy {item} attempt{attempt + 1}: "
                            f"{type(e).__name__} {str(e)[:60]}")
                        if time.monotonic() > _deadline:
                            break
                        time.sleep(8 + attempt * 4)
                raise RuntimeError(f"proxy budget reached, last error: {last}")

            EMSectorFlowHistoryAdapter.fetch = fetch_via_jina
            ad = EMSectorFlowHistoryAdapter(boards, 250)
            ad.min_interval = 10.0
            result = {}

            def _proxy_job():
                try:
                    result["st"] = runner.run(ad, job_key=f"em_flow_hist:jina:{today}")
                except Exception as e:  # noqa: BLE001
                    result["err"] = e

            th = threading.Thread(target=_proxy_job, daemon=True)
            th.start()
            th.join(PROXY_BUDGET + 60)
            if th.is_alive():
                log(f"history proxy: HARD TIMEOUT after {PROXY_BUDGET + 60}s — abandoned; "
                    "job lock self-heals after 2h (same-day rerun will report 'skipped')")
            elif "err" in result:
                log(f"history proxy: error: {result['err']}")
            else:
                st = result["st"]
                if st.get("skipped"):
                    log(f"history proxy: SKIPPED ({st.get('skipped')}) — "
                        "stale lock, self-heals in 2h or clear job_runs manually")
                else:
                    log(f"history proxy: {st.get('status')} ok={st.get('ok')} "
                        f"failed={st.get('failed')}")

    # 3. latest margin day
    st = runner.run(MarginDetailDayAdapter(dates=[today.replace("-", "")]),
                    job_key=f"margin_detail:{today}")
    log(f"margin: {st.get('status')} ok={st.get('ok')}")

    # 4. TDX dailies
    rc = sh(f"{sys.executable} {ROOT}/ingestion/ingest_tdx880.py")
    log(f"tdx880 ingest rc={rc}")

    # 5. recompute caches — INCREMENTAL by default (user: data is stable,
    #    refresh every few days on request; skip the ~3min SLSQP pass unless
    #    the cache is >3 days old, missing, or --full is passed)
    cache = ROOT / "data/p0_results/timeseries_cache.json"
    cache_age = ((dt.datetime.now() - dt.datetime.fromtimestamp(cache.stat().st_mtime)).days
                 if cache.exists() else 999)
    if "--full" in sys.argv or cache_age >= 3:
        from models.sector_panel import fund_exposure_timeseries, etf_state_equivalent
        t0 = time.time()
        with Session(engine) as s:
            fe = fund_exposure_timeseries(s)
            et = etf_state_equivalent(s)
        cache.write_text(json.dumps({"fund_exp": fe, "etf_state": et}, ensure_ascii=False))
        log(f"cache RECOMPUTED in {time.time()-t0:.0f}s (age was {cache_age}d)")
    else:
        log(f"cache skipped (age {cache_age}d < 3d; --full to force)")

    # 6. reload API
    sh("fuser -k 8120/tcp 2>/dev/null", 30)
    time.sleep(1)
    sh(f"cd {ROOT} && nohup {sys.executable} -m uvicorn api.app:app "
       "--host 0.0.0.0 --port 8120 > /tmp/capobs_api.log 2>&1 &", 10)
    time.sleep(4)
    rc = sh("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8120/api/v1/sectors/panels?top=5", 60)
    log(f"panels endpoint rc={rc}")
    log("refresh done -> http://154.64.231.1:8120/dashboard/")


if __name__ == "__main__":
    main(skip_history="--skip-history" in sys.argv)
