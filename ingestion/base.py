"""Ingestion contract layer per docs/TECHNICAL_DESIGN_V1.md sec.4.

Adapter = discover | fetch | parse | validate. Fetch writes raw artifacts;
parse+validate produce normalized records; NOTHING here writes the estimate
store. Jobs are idempotent by business key with run locks, retries, error
classification and cursors recorded in job_run.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from storage.models import Base, JobRun, RawArtifact, init_db

RETRYABLE = {"network", "timeout", "http_5xx", "http_4xx"}
MAX_RETRIES = 3
BACKOFF_BASE = 5.0  # seconds, with jitter


@dataclass
class Batch:
    """Adapter output contract (sec.4)."""
    source_id: str
    upstream_id: str
    batch_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    records: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_class: str = ""
    data_range: str = ""


def error_class(exc: BaseException) -> str:
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if any(k in msg for k in ("429", "too many")):
        return "http_429"
    if any(k in msg for k in ("403", "404", "401")):
        return "http_4xx"
    if any(k in msg for k in ("500", "502", "503", "504")):
        return "http_5xx"
    if "connection" in msg or "remote end closed" in msg or "ssl" in msg:
        return "network"
    return "parse"


class CollectionAdapter(ABC):
    """One dataset from one entry. Subclasses stay small & focused."""

    source_id: str = "abstract"
    upstream_id: str = "unknown"

    #: seconds between calls (be conservative; free sources)
    min_interval: float = 1.5
    _last: float = 0.0

    def throttle(self) -> None:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    # -- contract stages --------------------------------------------------
    @abstractmethod
    def discover(self) -> Iterable[str]:
        """Yield work items (dates, symbols, report periods...)."""

    @abstractmethod
    def fetch(self, item: str) -> dict:
        """Fetch one item -> {'payload': bytes|str, 'url': str, 'content_type': str}."""

    @abstractmethod
    def parse(self, item: str, raw: dict) -> list[dict]:
        """Parse one artifact -> normalized records (facts or disclosures)."""

    def validate(self, records: list[dict]) -> tuple[list[dict], list[str]]:
        """Structural validation; isolate (not ingest) bad batches."""
        warns = []
        good = []
        for r in records:
            if r.get("value") is None:
                warns.append(f"null value dropped: {r.get('metric')}@{r.get('effective_at')}")
                continue
            try:
                float(r["value"])
            except (TypeError, ValueError):
                warns.append(f"non-numeric {r.get('value')!r} for {r.get('metric')}")
                continue
            good.append(r)
        return good, warns


def archive_artifact(session: Session, dataset: str, name: str, payload: bytes | str,
                     url: str, upstream: str, content_type: str,
                     parser_version: str, root: Path) -> RawArtifact:
    """Persist artifact to disk + registry (hash-addressed, immutable)."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    chash = hashlib.sha256(payload).hexdigest()
    art_id = f"{dataset}:{name}"
    existing = session.execute(
        select(RawArtifact).where(RawArtifact.artifact_id == art_id)).scalar_one_or_none()
    if existing:
        if existing.content_hash == chash:
            return existing  # idempotent: identical content
        # same id, new content -> versioned artifact id
        art_id = f"{dataset}:{name}:{chash[:8]}"
    d = root / dataset
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}__{chash[:10]}.raw"
    path.write_bytes(payload)
    art = RawArtifact(artifact_id=art_id, content_hash=chash, path=str(path),
                      fetched_at=dt.datetime.now(dt.timezone.utc), url=url,
                      upstream_id=upstream, content_type=content_type,
                      parser_version=parser_version, bytes=len(payload))
    session.add(art)
    return art


class JobRunner:
    """Idempotent job execution with lock, retry/backoff-with-jitter, cursor."""

    def __init__(self, db_url: str, raw_root: Path):
        self.engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self.engine)
        self.raw_root = raw_root

    def run(self, adapter: CollectionAdapter, items: list[str] | None = None,
            job_key: str | None = None) -> dict:
        job_key = job_key or f"{adapter.source_id}:{dt.date.today().isoformat()}"
        lock = uuid.uuid4().hex[:12]
        with Session(self.engine) as s:
            jr = s.execute(select(JobRun).where(JobRun.job_key == job_key)).scalar_one_or_none()
            if jr is None:
                jr = JobRun(job_key=job_key, job_type=adapter.source_id, status="pending")
                s.add(jr)
                s.commit()
            if jr.status == "running":
                # stale-lock heuristic: >2h assume dead
                stale = (jr.started_at and
                         (dt.datetime.now(dt.timezone.utc) - jr.started_at).total_seconds() > 7200)
                if not stale:
                    return {"skipped": "running", "job_key": job_key}
            jr.status, jr.lock_token, jr.started_at = "running", lock, dt.datetime.now(dt.timezone.utc)
            jr.retries = jr.retries or 0
            s.commit()
            cursor = jr.cursor or ""
        items = items if items is not None else list(adapter.discover())
        done: list[str] = []
        summary = {"ok": 0, "failed": 0, "warnings": []}
        try:
            todo = [i for i in items if (not cursor or i > cursor)]
            for item in todo:
                retries = 0
                while True:
                    try:
                        adapter.throttle()
                        raw = adapter.fetch(item)
                        with Session(self.engine) as s1:  # short tx per item
                            art = archive_artifact(
                                s1, adapter.source_id.replace(".", "_"), item.replace("/", "_"),
                                raw["payload"], raw["url"], adapter.upstream_id,
                                raw.get("content_type", "application/octet-stream"),
                                getattr(adapter, "parser_version", "v1"), self.raw_root)
                            s1.flush()          # assign artifact_id
                            art_id_val = art.artifact_id
                            s1.commit()
                        recs = adapter.parse(item, raw)
                        good, warns = adapter.validate(recs)
                        summary["warnings"] += warns[:5]
                        for r in good:
                            r.setdefault("source_id", adapter.source_id)
                            r.setdefault("artifact_id", art_id_val)
                        if good:
                            from storage.ingest import write_records
                            with Session(self.engine) as sw:
                                wstats = write_records(sw, good)
                            summary.setdefault("writes", {}).update(wstats)
                        summary["ok"] += 1
                        done.append(item)
                        break
                    except Exception as exc:  # noqa: BLE001 — classify, maybe retry
                        ec = error_class(exc)
                        if ec in RETRYABLE and retries < MAX_RETRIES:
                            retries += 1
                            time.sleep(BACKOFF_BASE * (2 ** (retries - 1)) * (1 + random.random() / 2))
                            continue
                        summary["failed"] += 1
                        summary["warnings"].append(f"{item}: {ec}: {str(exc)[:120]}")
                        break
                with Session(self.engine) as s2:  # cursor after each item (crash-safe)
                    jr2 = s2.execute(select(JobRun).where(JobRun.job_key == job_key)).scalar_one()
                    jr2.cursor = item
                    s2.commit()
            status = "succeeded" if summary["failed"] == 0 else (
                "partial" if summary["ok"] > 0 else "failed")
            with Session(self.engine) as s3:
                jr3 = s3.execute(select(JobRun).where(JobRun.job_key == job_key)).scalar_one()
                jr3.status, jr3.finished_at = status, dt.datetime.now(dt.timezone.utc)
                jr3.data_range = f"{todo[0]}..{todo[-1]}" if todo else ""
                jr3.notes = json.dumps(summary, ensure_ascii=False)[:4000]
                s3.commit()
            summary.update({"status": status, "job_key": job_key, "processed": len(done)})
            return summary
        except Exception as exc:  # noqa: BLE001
            with Session(self.engine) as s4:
                jr4 = s4.execute(select(JobRun).where(JobRun.job_key == job_key)).scalar_one()
                jr4.status, jr4.error_kind = "failed", error_class(exc)
                jr4.notes = str(exc)[:1000]
                s4.commit()
            raise
