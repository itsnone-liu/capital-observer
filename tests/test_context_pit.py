import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api.app import app, engine
from storage.models import FactObservation


def test_context_excludes_fact_available_after_asof():
    marker = "bk:pit_board"
    with Session(engine) as s:
        s.add(FactObservation(source_id="test", artifact_id="test:pit",
            subject_key=marker, asset_key=None, metric="sf_main_net", value=123,
            unit="yuan", effective_at="2026-01-01", published_at=None,
            available_at="2026-01-02T12:00:00", quality_status="valid"))
        s.commit()
    client = TestClient(app)
    try:
        before = client.get("/api/v1/context", params={"board": "pit_board",
                            "as_of": "2026-01-02T11:59:59"}).json()
        after = client.get("/api/v1/context", params={"board": "pit_board",
                           "as_of": "2026-01-02T12:00:00"}).json()
        assert "sector:em_board_flow" not in before["channels"]
        assert "sector:em_board_flow" in after["channels"]
        assert before["as_of"] == "2026-01-02T11:59:59"
    finally:
        with Session(engine) as s:
            s.query(FactObservation).filter(FactObservation.artifact_id == "test:pit").delete()
            s.commit()


def test_context_batch_keeps_one_asof():
    client = TestClient(app)
    payload = {"as_of": "2026-01-01T00:00:00",
               "items": [{"code": "600001", "board": "BK0001"},
                         {"code": "600002", "board": "BK0002"}]}
    r = client.post("/api/v1/context/batch", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert all(x["as_of"] == payload["as_of"] for x in body["items"])
