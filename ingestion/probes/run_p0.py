"""P0 runner: execute all probes sequentially (cross-process rate safety),
then assemble the source_capability matrix + gap report.

Usage: python3 run_p0.py [--probes p1,p2] [--summarize-only]
"""
from __future__ import annotations

import json
import runpy
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RESULTS = ROOT / "data" / "p0_results"

ALL_PROBES = ["index_market", "etf_share", "margin", "fund_holdings", "index_cons", "baostock"]


def run(probes: list[str]) -> None:
    for name in probes:
        script = HERE / f"probe_{name}.py"
        if not script.exists():
            print(f"!! no probe script for {name}", flush=True)
            continue
        print(f"\n===== probe {name} =====", flush=True)
        t0 = time.time()
        try:
            runpy.run_path(str(script), run_name="__main__")
        except Exception as exc:  # noqa: BLE001
            print(f"!! probe {name} crashed hard: {exc}", flush=True)
        print(f"===== {name} done in {time.time()-t0:.1f}s =====", flush=True)


def summarize() -> None:
    rows = []
    for f in sorted(RESULTS.glob("*.json")):
        doc = json.loads(f.read_text())
        for c in doc.get("checks", []):
            rows.append({
                "dataset": doc["dataset"],
                "check": c.get("check"),
                "upstream": c.get("upstream"),
                "adapter_entry": c.get("adapter_entry"),
                "status": c.get("status"),
                "rows": c.get("rows", ""),
                "date_range": "→".join(c.get("date_range", [])) if c.get("date_range") else "",
                "error_kind": c.get("error_kind", ""),
                "error": (c.get("error", "") or "")[:120],
                "artifact": (c.get("artifact", {}) or {}).get("artifact_id", ""),
                "note": c.get("note", ""),
            })

    import csv
    out_csv = RESULTS / "source_capability_matrix.csv"
    if rows:
        with open(out_csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    # markdown twin for review
    out_md = RESULTS / "source_capability_matrix.md"
    lines = ["# P0 source_capability 矩阵（自动生成）", "",
             "| dataset | check | upstream | status | rows | date_range | note |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["check"] == "api_discovery":
            continue
        lines.append(f"| {r['dataset']} | {r['check']} | {r['upstream']} | **{r['status']}** "
                     f"| {r['rows']} | {r['date_range']} | {r['note'].replace('|', '/')} |")
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    lines += ["", f"状态统计: {by_status}", ""]
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"summarized {len(rows)} checks -> {out_csv.name} / {out_md.name}", flush=True)
    print(json.dumps(by_status, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--summarize-only" in args:
        summarize()
        sys.exit(0)
    probes = ALL_PROBES
    for a in args:
        if a.startswith("--probes"):
            probes = a.split("=", 1)[1].split(",")
    run(probes)
    summarize()
