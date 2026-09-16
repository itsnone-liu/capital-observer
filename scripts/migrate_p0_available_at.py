#!/usr/bin/env python3
"""P0 additive migration: fact_observation.available_at.

Existing rows remain NULL: historical public availability is unknown and must not be
fabricated. New ingestion records conservative retrieval time via JobRunner.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "capobs.db"


def main() -> None:
    conn = sqlite3.connect(DB)
    cols = {r[1] for r in conn.execute("pragma table_info(fact_observation)")}
    if "available_at" not in cols:
        conn.execute("alter table fact_observation add column available_at varchar(32)")
        conn.execute("create index if not exists ix_fact_available on fact_observation(available_at)")
        print("added fact_observation.available_at")
    # P0审计确认存量fact的published_at全部由adapter复制effective_at，非真实披露时间。
    # 清空而不是编造；position_disclosure不在本迁移范围内。
    n = conn.execute("select count(*) from fact_observation where published_at is not null").fetchone()[0]
    conn.execute("update fact_observation set published_at=null where published_at is not null")
    conn.commit()
    print(f"cleared {n} unverified published_at values; existing available_at kept NULL")
    conn.close()


if __name__ == "__main__":
    main()
