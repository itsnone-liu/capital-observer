# ETF 官方日度份额通道（2026-09-15 上线）

免费交易所直连（无凭据），T 晚清算后发布（T+1 早可得的事实层）。

## 数据道

| 环节 | 内容 |
|---|---|
| 采集 | `ingestion/adapters/etf_share.py`：SSE `fund_etf_scale_sse(date)` 逐日快照；SZSE `fund_scale_daily_szse` 区间 xlsx（≤150天/段，`\x00` 分段） |
| 存储 | `fact_observation`：metric=`etf_total_shares`，unit=shares，subject=`etf:{code}` |
| 刷新 | `scripts/refresh.py` 步骤3.5 增量道：`--skip-etf` 跳过，`--etf-backfill=N` 冷启动回看 |
| API | `GET /api/v1/etf/shares?code=&days=`：同源差分 share_change，`estimated_net_flow=Δ×当日净值`（nav 缺失→null 不外推） |
| 消费 | v2 stock-selector `decide` 自动经 `/api/v1/context` 消费（宽基ETF通道） |

## 契约要点

- **同源差分**：沪/深清算时点不同，绝不跨源算 Δ；
- **T+1 事实**：当日盘中不存在此事实，`/api/v1/context` 里标注 T-1；
- SSE 当日未清算 → 空表（adapter 返回空 payload，0 条，无错误）；
- SZSE 偶发 ConnectionReset → adapter 内 2 次重试即过。

## 踩坑存档

多段 CSV 载荷不能用 `"\n"` join/split——CSV 自身含换行，按行拆会把每行
当独立 CSV、首行数据成表头，几千个互异列名帧 concat 出 帧数×列数 稀疏
矩阵，parse 阶段 CPU 满转假死。必须用数据中不存在的分隔符（`\x00`）。
（`tests/test_etf_share_adapter.py` 有回归测试。）

## 2026-09-16 更新：净创设口径 + 历史回补

**口径修正（评审发现）**：宽基 ETF 综合方向曾按 ΣΔ份额 聚合——不同 ETF 一份的
经济价值不同（NAV 差一个数量级），份额不可加。已改为 **net_creation_value
= Σ(Δ份额 × NAV)**，语义为"净创设/赎回价值"（实物申赎口径，新增 ETF 暴露
规模），不等于二级市场现金买股金额。NAV 回退链：etf_nav(≤date 最新) →
etf_close。单 ETF 明细（delta_shares/nav_used/nav_source/value）随通道输出。

**context v0.2-layered**：市场层（宽基净创设 + 沪/深两融分立通道判同向）与
行业层（板块大单 proxy；board_margin/industry_etf 规划位计入 coverage 分母）。
coverage = 已知/预期（如 4/6），缺失不再被分母隐藏。

**历史回补**：`scripts/backfill_etf_shares.py --from 2024-01-02`——SSE 逐日
+ SZSE 150天分块，幂等 upsert，回补至 2-3 年供历史验证使用。
