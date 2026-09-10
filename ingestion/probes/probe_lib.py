"""P0 probe infrastructure: rate limiting, artifact archiving, result recording.

Per docs/TECHNICAL_DESIGN_V1.md sec.3 (source_capability matrix) and sec.4
(collection contract). Probes are read-only, respect rate limits, never
bypass captcha; failures are recorded as data, not crashes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "data" / "p0_results"
SAMPLES_DIR = ROOT / "data" / "raw_samples" / "p0"

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# --- Eastmoney WAF resets connections carrying the default python UA.  -----
# Inject a browser UA on every requests call library-wide (discovered in P0,
# 2026-09-10: bare requests -> RemoteDisconnected; browser UA -> 200).
import requests as _rq  # noqa: E402
import socket as _socket  # noqa: E402

_socket.setdefaulttimeout(25)  # requests has no default timeout; a hung
# upstream (csindex accept-then-silence, P0 2026-09-10) would block forever.

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_orig_request = _rq.Session.request


def _patched_request(self, method, url, **kw):
    headers = kw.pop("headers", None) or {}
    headers.setdefault("User-Agent", _UA)
    kw["headers"] = headers
    kw.setdefault("timeout", 20)
    return _orig_request(self, method, url, **kw)


_rq.Session.request = _patched_request

_MIN_INTERVAL = 1.2  # seconds between calls to the SAME upstream; conservative
_LAST_CALL: dict[str, float] = {}

# error taxonomy per design sec.4 (error classification)
ERROR_KINDS = ("network", "http_4xx", "http_5xx", "parse", "empty", "schema", "timeout", "other")


def rate_limit(key: str, interval: float = _MIN_INTERVAL) -> None:
    now = time.monotonic()
    last = _LAST_CALL.get(key, 0.0)
    wait = last + interval - now
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL[key] = time.monotonic()


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def classify_error(exc: BaseException) -> str:
    msg = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in msg or "timed out" in msg:
        return "timeout"
    if any(k in msg for k in ("429", "too many requests")):
        return "http_4xx"
    if any(k in msg for k in ("403", "404", "401")):
        return "http_4xx"
    if any(k in msg for k in ("500", "502", "503", "504")):
        return "http_5xx"
    if isinstance(exc, (KeyError, IndexError, ValueError, TypeError)):
        return "parse"
    if "connection" in msg or "network" in msg or "ssl" in msg:
        return "network"
    return "other"


def save_artifact(dataset: str, name: str, payload: bytes | str, url: str, upstream: str,
                  content_type: str = "application/json", parser_version: str = "p0-probe/1") -> dict:
    """Archive a raw artifact with metadata per design sec.4 (raw artifact)."""
    d = SAMPLES_DIR / dataset
    d.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    content_hash = hashlib.sha256(payload).hexdigest()
    ext = ".json" if "json" in content_type else (".csv" if "csv" in content_type else ".bin")
    path = d / f"{name}{ext}"
    path.write_bytes(payload)
    meta = {
        "artifact_id": f"{dataset}:{name}",
        "content_hash": content_hash,
        "path": str(path.relative_to(ROOT)),
        "fetched_at": _now_iso(),
        "url": url,  # keys stripped by caller
        "upstream": upstream,
        "content_type": content_type,
        "parser_version": parser_version,
        "bytes": len(payload),
    }
    (d / f"{name}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return meta


def summarize_df(df: Any) -> dict:
    """Describe a pandas DataFrame probe result without dumping it all."""
    n_rows, n_cols = int(df.shape[0]), int(df.shape[1])
    cols = [str(c) for c in df.columns.tolist()]
    dtypes = {str(c): str(t) for c, t in df.dtypes.items()}
    head = {}
    try:
        head = df.head(3).to_dict(orient="records")
        head = json.loads(json.dumps(head, ensure_ascii=False, default=str))
    except Exception:
        pass
    return {"rows": n_rows, "cols": n_cols, "columns": cols, "dtypes": dtypes, "head": head}


class Probe:
    """One probe = one dataset capability check producing one result entry."""

    def __init__(self, dataset: str):
        self.dataset = dataset
        self.checks: list[dict] = []

    def check(self, name: str, upstream: str, entry: str, fn: Callable[[], Any],
              expect_min_rows: int = 1, note: str = "") -> dict:
        """Run fn() (returns DataFrame or list-like + optional artifact saver)."""
        rec: dict[str, Any] = {
            "check": name, "upstream": upstream, "adapter_entry": entry,
            "tested_at": _now_iso(), "note": note,
        }
        attempt, last_exc = 0, None
        for attempt in (1, 2, 3):  # EM WAF resets are intermittent: retry w/ backoff
            try:
                rate_limit(upstream, interval=_MIN_INTERVAL + attempt)
                out = fn()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if classify_error(exc) in ("parse", "schema") or attempt == 3:
                    break
                time.sleep(2.0 * attempt)
        try:
            if last_exc is not None:
                raise last_exc
            if isinstance(out, tuple) and len(out) == 2 and isinstance(out[1], dict):
                df, artifact_meta = out
            else:
                df, artifact_meta = out, None
            n = int(df.shape[0]) if hasattr(df, "shape") else len(df) if df is not None else 0
            rec["status"] = "VERIFIED" if n >= expect_min_rows else ("EMPTY" if n == 0 else "PARTIAL")
            rec["rows"] = n
            rec["expected_min_rows"] = expect_min_rows
            if hasattr(df, "columns"):
                rec["summary"] = summarize_df(df)
            elif isinstance(df, list) and df:
                rec["summary"] = {"items": len(df), "head": df[:3]}
            if artifact_meta:
                rec["artifact"] = {k: artifact_meta[k] for k in
                                   ("artifact_id", "content_hash", "bytes", "fetched_at")}
            # date range detection for frame-like data
            try:
                datecols = [c for c in df.columns if str(c).lower() in
                            ("日期", "date", "净值日期", "交易日期", "公告日期")]
                if datecols and n > 0:
                    c = datecols[0]
                    rec["date_range"] = [str(df[c].iloc[0]), str(df[c].iloc[-1])]
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001 - probe must record, not crash
            rec["status"] = "UNAVAILABLE"
            rec["error_kind"] = classify_error(exc)
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
            rec["trace_tail"] = traceback.format_exc().strip().splitlines()[-1][:200]
        self.checks.append(rec)
        print(f"[{self.dataset}] {name}: {rec['status']}"
              + (f" rows={rec.get('rows')}" if "rows" in rec else "")
              + (f" err={rec.get('error_kind')}" if "error_kind" in rec else ""), flush=True)
        return rec

    def finish(self, extra: dict | None = None) -> dict:
        doc = {"dataset": self.dataset, "finished_at": _now_iso(), "checks": self.checks}
        if extra:
            doc["notes"] = extra
        out = RESULTS_DIR / f"{self.dataset}.json"
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str))
        print(f"[{self.dataset}] wrote {out.relative_to(ROOT)} with {len(self.checks)} checks", flush=True)
        return doc


def df_artifact(df, dataset: str, name: str, url: str, upstream: str) -> tuple[Any, dict]:
    """Pair a DataFrame with its raw artifact save (csv text form)."""
    import io
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    meta = save_artifact(dataset, name, buf.getvalue(), url, upstream, content_type="text/csv")
    return df, meta
