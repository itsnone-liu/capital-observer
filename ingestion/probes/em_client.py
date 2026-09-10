"""Direct eastmoney client (P0 finding: akshare's 80.push2 node is RST-blocked
from this network; other EM nodes intermittently reachable).

Strategy per design sec.4: multi-node rotation + exponential backoff +
conservative rate limit. Only public quote/board endpoints; browser UA;
no auth, no scraping behind captcha. If all nodes fail -> raise (caller
records UNAVAILABLE honestly; never fabricate).
"""
from __future__ import annotations

import random
import time

import pandas as pd
import requests

NODES = ["push2delay.eastmoney.com", "push2his.eastmoney.com",
         "80.push2.eastmoney.com", "1.push2.eastmoney.com",
         "2.push2.eastmoney.com", "push2.eastmoney.com"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_last = 0.0
_MIN = 1.0


def _throttle():
    global _last
    wait = _last + _MIN - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()


def _get(path: str, params: dict, tries: int = 6, accept=None) -> dict:
    """GET across nodes with backoff; returns parsed JSON.

    accept(js) -> False treats a 200-but-empty/invalid payload as node failure
    and rotates (push2delay happily 200s with empty kline payloads).
    """
    nodes = NODES[:]
    random.shuffle(nodes)
    last_exc: Exception | None = None
    for i, host in enumerate(nodes[:tries]):
        _throttle()
        url = f"https://{host}{path}"
        try:
            r = requests.get(url, params=params,
                             headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"},
                             timeout=15)
            if r.status_code == 200 and r.text.strip():
                js = r.json()
                if accept is None or accept(js):
                    return js
                last_exc = RuntimeError(f"{host} empty payload")
            else:
                last_exc = RuntimeError(f"{host} http {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(1.0 * (i + 1) + random.random())  # backoff w/ jitter
    raise ConnectionError(f"all EM nodes failed for {path}: {last_exc}")


def secid(code: str) -> str:
    """Build secid: 1.=SH, 0.=SZ, 90.=EM board."""
    code = code.upper()
    if code.startswith("BK"):
        return f"90.{code}"
    if code.startswith(("SH", "1.")):
        return code if "." in code else f"1.{code[2:]}"
    if code.startswith(("SZ", "0.")):
        return code if "." in code else f"0.{code[2:]}"
    # bare numeric: infer exchange by code range
    if code.startswith(("6", "9", "5")):  # SH stock/fund/index(000xxx SH index too)
        if code.startswith("000") and len(code) == 6:
            return f"1.{code}"  # SH index 000300 etc
        return f"1.{code}"
    return f"0.{code}"


def kline_daily(sec: str, start: str = "19900101", end: str = "20991231",
                fqt: int = 1) -> pd.DataFrame:
    """Daily bars fqt: 0 raw / 1 qfq / 2 hfq."""
    params = {
        "secid": secid(sec), "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": str(fqt), "beg": start, "end": end,
    }
    js = _get("/api/qt/stock/kline/get", params,
              accept=lambda j: bool((j.get("data") or {}).get("klines")))
    data = (js.get("data") or {})
    rows = [ln.split(",") for ln in (data.get("klines") or [])]
    cols = ["date", "open", "close", "high", "low", "volume", "amount",
            "amplitude", "pct_chg", "chg", "turnover"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows, columns=cols)
    return df


def board_list() -> pd.DataFrame:
    """EM industry board list (fs=m:90+t:2 industry)."""
    params = {
        "pn": "1", "pz": "200", "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f3", "fs": "m:90+t:2",  # industry boards
        "fields": "f12,f14,f3,f104,f105,f128,f140,f141,f207,f208",
    }
    js = _get("/api/qt/clist/get", params)
    total = (js.get("data") or {}).get("total", 0)
    if total > 200:
        params["pz"] = str(total)
        js = _get("/api/qt/clist/get", params)
    rows = (js.get("data") or {}).get("diff") or []
    return pd.DataFrame(rows)


def board_cons(board_code: str) -> pd.DataFrame:
    """Members of one EM board via clist (push2delay-friendly)."""
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f12", "fs": f"b:{board_code}",
        "fields": "f12,f13,f14,f2,f3,f20,f21",
    }
    js = _get("/api/qt/clist/get", params)
    total = (js.get("data") or {}).get("total", 0)
    all_rows: list[dict] = []
    pn = 1
    while len(all_rows) < min(total, 5000):
        params["pn"] = str(pn)
        js = _get("/api/qt/clist/get", params)
        diff = (js.get("data") or {}).get("diff") or []
        if not diff:
            break
        all_rows.extend(diff)
        pn += 1
    return pd.DataFrame(all_rows)


def etf_list() -> pd.DataFrame:
    """All exchange-listed ETFs via clist (EM fund boards MK0021..)."""
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f12",
        "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0025,b:MK0026,b:MK0027,b:MK0028",
        "fields": "f12,f13,f14,f2,f3,f5,f6,f20,f21",
    }
    js = _get("/api/qt/clist/get", params)
    total = (js.get("data") or {}).get("total", 0)
    all_rows: list[dict] = []
    pn = 1
    while len(all_rows) < min(total, 3000):
        params["pn"] = str(pn)
        js = _get("/api/qt/clist/get", params)
        diff = (js.get("data") or {}).get("diff") or []
        if not diff:
            break
        all_rows.extend(diff)
        pn += 1
    return pd.DataFrame(all_rows)


def fund10_holdings(code: str, year: int = 2025) -> pd.DataFrame:
    """Quarterly equity holdings from fundf10 jjcc HTML (top-10 per quarter).

    akshare's fund_portfolio_hold_em adapter broke (JSONDecode ';') while the
    upstream page works; parse tables directly.
    """
    import io
    r = requests.get(
        "https://fundf10.eastmoney.com/FundArchivesDatas.aspx",
        params={"type": "jjcc", "code": code, "topline": "10",
                "year": str(year), "month": ""},
        headers={"User-Agent": UA, "Referer": "https://fundf10.eastmoney.com/"},
        timeout=15)
    r.raise_for_status()
    try:
        tables = pd.read_html(io.StringIO(r.text))
    except ValueError as exc:
        raise RuntimeError(f"jjcc page has no table for {code}/{year}: {str(exc)[:80]}") from exc
    return tables[0] if tables else pd.DataFrame()


def fund10_share_change(code: str) -> pd.DataFrame:
    """ETF/open-fund quarterly share change from fundf10 (gmbd) HTML.

    Parses `var apidata={ content:"<table>..."}` — the page embeds per-quarter
    期初份额/期末份额/申购/赎回. Quarterly granularity only (P0 confirmed).
    """
    import io
    js = requests.get(
        "https://fundf10.eastmoney.com/FundArchivesDatas.aspx",
        params={"type": "gmbd", "code": code},
        headers={"User-Agent": UA, "Referer": "https://fundf10.eastmoney.com/"},
        timeout=15)
    js.raise_for_status()
    text = js.text
    try:
        tables = pd.read_html(io.StringIO(text))
    except ValueError as exc:
        raise RuntimeError(f"gmbd page has no table for {code}: {str(exc)[:80]}") from exc
    return tables[0] if tables else pd.DataFrame()


def spot_all_a() -> pd.DataFrame:
    """All A-share snapshot (mcap for margin ratios) via clist paging."""
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f12",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
        "fields": "f12,f13,f14,f2,f3,f5,f6,f20,f21",
    }
    js = _get("/api/qt/clist/get", params)
    total = (js.get("data") or {}).get("total", 0)
    all_rows: list[dict] = []
    pn = 1
    while len(all_rows) < min(total, 7000):
        params["pn"] = str(pn)
        js = _get("/api/qt/clist/get", params)
        diff = (js.get("data") or {}).get("diff") or []
        if not diff:
            break
        all_rows.extend(diff)
        pn += 1
    return pd.DataFrame(all_rows)
