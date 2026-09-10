# P0 免费数据源能力验证报告（V1.0 终稿）

日期：2026-09-10 | 依据：TECHNICAL_DESIGN_V1.md sec.3 | 探测：45项检查（37✓/2△/5✗/1空），全部可复现

## 1. 结论摘要

核心判断：**免费源足以支撑 P2 观测版主体与 P3 估计版大部分**。所有 5 个 UNAVAILABLE 项均有
已 VERIFIED 的替代通道（明细见第4节）；2 个 PARTIAL（中证1000 自2021年2895行、科创50 自2020年
1622行）均满足"两年以上历史"目标。唯一实质性数据缺口是 ETF 日度份额序列（仅季度粒度），降级
方案符合设计文档的 L1/L2/L3 分档框架。

## 2. 网络/上游重大发现（影响架构）

| 发现 | 影响 | 对策（已实施） |
|---|---|---|
| 东财 push2 系节点对本机网络 RST（TLS/连接层，curl_cffi 亦被拦，curl 命令行间歇可用） | kline/clist 类接口不稳定 | 行情改新浪主源+申万+baostock 三独立源；clist 类走 push2delay 节点轮询直连客户端 `em_client` |
| akshare 部分适配层损坏（fund_portfolio_hold_em JSONDecodeError ';'）而上游页面正常 | 不能盲信适配器 | 关键路径自写解析（fundf10 jjcc/gmbd HTML 表格解析，em_client） |
| python requests 无默认超时 → csindex 挂死 | 探测/采集卡死 | 全局 socket 超时 25s + 请求级 20s |
| fundf10.eastmoney.com / datacenter-web / fundapi 三个域名族未被拦 | 持仓/份额变动/基金数据可用 | 直连+浏览器UA |

## 3. 已验证数据源（终轮探测矩阵摘录，全部可复现：`python3 ingestion/probes/run_p0.py`）

| 数据集 | 上游 | 入口 | 状态 | 历史 | 说明 |
|---|---|---|---|---|---|
| 宽基/风格指数日K×7 | 新浪 | ak.stock_zh_index_daily | ✓5项+△2项 | 2002起/最短2020起 | 沪深300 5989行、中证500 5268、上证50 5511、创业板3954、中证2000 4144；△中证1000 2895(2021起)、科创50 1622(2020起，发布即全量) |
| 指数日K对照 | baostock | query_history_k_data_plus(adjustflag) | ✓755行 | 多年 | 独立上游#2 |
| 申万一级行业指数 | 申万研究 | index_hist_sw | ✓6452行 | 1999起全量 | 行业主线 |
| 申万行业估值快照 | 申万研究 | sw_index_first_info | ✓31行 | 当日 | PE/PB/股息率 |
| 申万行业成分映射 | 申万研究 | index_component_sw | ✓493行@801080 | 快照 | effective dating 自建 |
| CSRC行业映射 | baostock | query_stock_industry | ✓5550行 | 快照 | 备用分类 |
| 沪深300成分+权重 | 中证官网 | index_stock_cons(_weight)_csindex | ✓300+300 | 月度快照 | 穿透权重 |
| 上交所两融汇总 | 上交所 | stock_margin_sse | ✓755行+深度868行 | 2020起 | |
| 上交所两融明细 | 上交所 | stock_margin_detail_sse | ✓1999行/日 | 单日文件 | 行业集中度原料 |
| 深交所两融明细 | 深交所 | stock_margin_detail_szse | ✓2104行/日 | 单日文件 | |
| 深交所两融汇总 | 深交所 | stock_margin_szse(单日) | ✓3日循环验证 | 单日 | 逐日循环采集 |
| ETF全清单 | 东财clist | em_client.etf_list | ✓1746行19s | 当日 | push2delay 通道 |
| ETF 场内K线 | 新浪 | fund_etf_hist_sina | ✓3474行@510300 | 2012起全量 | 含成交量 |
| ETF 官方净值 | 东财fundapi | fund_open_fund_info_em | ✓1922行 | 8年 | |
| ETF/基金份额变动(季度) | 东财fundf10 | gmbd 自写解析 | ✓38KB | 季度 | **缺口:非日度** |
| 基金季度重仓 | 东财fundf10 | jjcc 自写解析 | ✓10行/季 | 季度 | akshare适配器坏，自写通道通 |
| 基金份额/规模季度序列 | 东财 | fund_scale_change_em() | ✓113行 | 季度 | A/C份额归并下游处理 |
| 基金行业配置 | 东财 | fund_portfolio_industry_allocation_em | ✓42行 | 季度 | |
| 开放基金全名单 | 东财 | fund_open_fund_rank_em | ✓20301行 | 当日 | 抽样框 |
| 全A快照(市值) | 东财clist | em_client.spot_all_a | ✓5912行 | 当日 | 两融比例分母 |
| 行业板块列表+成分 | 东财clist | em_client.board_list/board_cons | ✓100板块/186成分 | 当日 | 辅助口径 |

## 4. 缺口与降级方案（诚实申报，不暗中替代）

| 缺口 | 影响 | 降级方案 |
|---|---|---|
| **ETF 日度份额序列无免费源**（fundf10 只有季度申购/赎回） | ΔS×NAV_t 日度净申赎不可直接计算 | L1: 季度份额+日度净值分解（宽区间）；L2: 场内成交量+申赎单位推断（identification_bound）；L3: 标记不可识别。首版以20/60日窗口聚合弱化日度要求 |
| 东财 push2 kline 被拦（kline canary、板块历史K线 ✗） | 板块历史K线缺一路 | 申万31行业1999起全历史已覆盖主线；东财板块仅当日快照辅助 |
| 指数权重历史版本 | 回放式穿透需历史权重 | 中证月度快照从启用日起逐月归档自建（不回填编造） |
| akshare fund_portfolio_hold_em 适配器损坏（3项✗） | — | fundf10 jjcc 自写解析已 VERIFIED（10行/季），年报期 topline 拉满可试全持仓 |
| em_datacenter 两融 reportName 待修正（EMPTY，非关键） | — | 交易所直连已全 VERIFIED，此项仅作冗余 |
| 深交所两融无区间API | 历史回补逐日循环 | 采集层 discover 按交易日历循环（base.py 已设计） |

## 5. 首批资产名单

见 configs/first_batch.yaml（宽基7只指数+20-30只ETF+50-100只主动权益抽样规则+5个重点行业）。

## 6. 历史回补与日常更新方案（草案）

- 回补：一次性 job 按 discover 顺序循环（新浪指数/申万全历史立即回补；两融按交易日循环回补 2024-09 起）
- 日常：APScheduler 单实例（jobs/scheduler.py），收盘后 T 日 17:30 指数+行业，T+1 早两融，季报后 22 工作日拉持仓
- 幂等与断点：job_run 游标 + artifact 哈希去重（已实现并冒烟验证）

## 7. P0 交付物清单

- [x] 数据能力矩阵 data/p0_results/source_capability_matrix.{csv,md}（45项检查终稿）
- [x] 可复现探测脚本 ingestion/probes/（含 em_client 直连客户端）
- [x] 原始样本 data/raw_samples/p0/（含哈希与元数据）
- [x] 缺口报告（本文档第4节）
- [x] 首批资产名单草案 configs/first_batch.yaml
- [x] 回补/更新方案（第6节）

## 8. P0 附带交付（超出计划部分）

- storage/models.py：sec.6 全部16表 SQLAlchemy 模型（dev=SQLite / prod=PG 双兼容）
- ingestion/base.py：sec.4 采集契约（JobRunner 幂等/游标/重试/退避/锁）
- ingestion/adapters/：新浪指数、申万行业两个正式适配器
- tests/test_ingestion_smoke.py：端到端冒烟（✓ 1640 facts 入库、幂等重跑验证）
- jobs/scheduler.py：APScheduler 单实例调度骨架（cron 按来源发布时间）
