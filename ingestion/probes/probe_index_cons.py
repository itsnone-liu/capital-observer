"""P0 probe: index constituents & weights, industry classification map.

Design sec.3: 指数权重/行业分类保留历史版本; sec.5: stock industry mapping
with effective_from/to; sec.7.1:穿透使用当时有效权重.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_lib import Probe, df_artifact  # noqa: E402

import akshare as ak  # noqa: E402

P = Probe("index_cons")

names = [n for n in dir(ak) if n.startswith("index_stock_cons") or "cons_weight" in n]
print("[index_cons] candidate apis:", names, flush=True)
P.check("api_discovery", "meta", "dir(ak)", lambda: names, expect_min_rows=3)

# ---- 1. CSI constituent list (中证指数官网) -----------------------------
def _cons300():
    df = ak.index_stock_cons_csindex(symbol="000300")
    return df_artifact(df, "index_cons", "cons_000300",
                       "akshare:index_stock_cons_csindex (upstream: csindex.com.cn)", "csindex")
P.check("csi300_constituents", "csindex", "akshare.index_stock_cons_csindex", _cons300,
        expect_min_rows=280)

# ---- 2. CSI constituent WEIGHTS (穿透权重关键) ---------------------------
def _w300():
    df = ak.index_stock_cons_weight_csindex(symbol="000300")
    return df_artifact(df, "index_cons", "cons_weight_000300",
                       "akshare:index_stock_cons_weight_csindex (upstream: csindex.com.cn)", "csindex")
P.check("csi300_weights", "csindex", "akshare.index_stock_cons_weight_csindex", _w300,
        expect_min_rows=280, note="free-float weights; historical VERSIONS may not be free — gap risk")

# ---- 3. SW industry constituents (行业分类映射源) -------------------------
def _sw_cons():
    fn = getattr(ak, "index_component_sw", None)
    if fn is None:
        raise KeyError("no index_component_sw in this build")
    df = fn(symbol="801080")
    return df_artifact(df, "index_cons", "sw_component_801080",
                       f"akshare:{fn.__name__} (upstream: swsresearch.com)", "swsresearch")
P.check("sw_industry_component_map", "swsresearch", "akshare.index_component_sw", _sw_cons,
        expect_min_rows=50, note="stock->SW industry map; effective dating handled downstream")

# ---- 4. EM board constituents (industry map alternative) ------------------
def _em_cons():
    df = ak.stock_board_industry_cons_em(symbol="半导体")
    return df_artifact(df, "index_cons", "em_cons_semiconductor",
                       "akshare:stock_board_industry_cons_em (upstream: eastmoney)", "eastmoney")
P.check("em_board_constituents", "eastmoney", "akshare.stock_board_industry_cons_em", _em_cons,
        expect_min_rows=30, note="EM board membership; snapshot only — history needs archiving")

# ---- 5. stock list w/ industry column (universe & join key) ---------------
def _stock_list():
    df = ak.stock_zh_a_spot_em()
    cols = [c for c in df.columns if c in ("代码", "名称", "总市值", "流通市值")]
    df2 = df[["代码", "名称"] + cols[2:]] if len(cols) >= 2 else df
    return df_artifact(df2, "index_cons", "a_stock_spot_snapshot",
                       "akshare:stock_zh_a_spot_em (upstream: eastmoney)", "eastmoney")
P.check("a_stock_universe_snapshot", "eastmoney", "akshare.stock_zh_a_spot_em", _stock_list,
        expect_min_rows=4000, note="mcap for margin ratios; join key for industry aggregation")

# ---- 6. index basic info from CSI (classification versions) ----------------
def _csi_info():
    df = ak.index_detail_info_cni(symbol="399006") if hasattr(ak, "index_detail_info_cni") else None
    if df is None:
        # fallback: CSI index detail via stock_zh_index_value_csindex? just record absence
        raise KeyError("no cni detail api; try csindex value api")
    return df_artifact(df, "index_cons", "cni_detail_399006",
                       "akshare:index_detail_info_cni (upstream: cnindex)", "cnindex")
P.check("cni_index_detail", "cnindex", "akshare.index_detail_info_cni", _csi_info,
        note="secondary index issuer coverage")

P.finish({
    "goal": "constituent+weight snapshots for look-through; HISTORICAL weight versions flagged as gap risk",
    "risk": "csindex historical weight files may need paid/registration; archive each month going forward",
})
