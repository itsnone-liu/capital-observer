# A股资金结构观测系统：技术设计 V1.0

状态：方案文档；未开始采集、模型开发或部署。
依据：用户确认面向大盘与板块，免费源优先，持仓与成本提供L1谨慎、L2基准、L3扩展判断。不做个股推荐及历史相似收益预测。

## 1. 产品契约

输出：大盘权益仓位变化、行业配置与扩散、ETF申赎、杠杆参与、条件化成本带、情景分歧与验证条件。
底层允许使用个股数据完成行业汇总，但不提供个股选股页面。
未知不填零；事实不因情景改变；估计不覆盖披露；AI不制造数值。

首版：代表性宽基、成长市场指数、风格、一级行业。二级行业依数据覆盖再扩展。首批验证20–30只ETF、50–100个主动权益组合、3–5个行业，目标争取两年以上历史；不能取得的区间公开缺口。

## 2. 架构与技术选择

模块化单体：采集→原始归档→标准化/质检→事实库→估计模型→情景引擎→估算库→API/看板/AI报告。
Python + FastAPI；Polars/Pandas、NumPy、SciPy、statsmodels/sklearn，约束优化按需引入CVXPY。
PostgreSQL存应用数据与版本索引；Parquet存标准化批量数据；DuckDB用于研究查询；原始JSON/HTML/PDF独立归档。
React + TypeScript + ECharts；独立单实例APScheduler调度进程，PostgreSQL记录任务。API worker不启动调度器。暂不引入TimescaleDB、Redis/Celery、图数据库、消息队列。
AI独立适配模块，不是必需运行依赖；停用时保留模板报告。

## 3. 免费数据能力验证（P0必须先行）

候选：交易所公开行情与两融、基金公司净值/报告/ETF披露、巨潮与交易所公告、指数公开资料；AKShare/BaoStock及东方财富/天天基金等公开入口用于辅助。此列表不代表接口已经可用或有再分发许可。
建立source_capability清单：数据集、原始上游、适配入口、权限条款、历史范围、频率、公布延迟、单位、样本缺失、修订方式、备用源、测试日期、VERIFIED/PARTIAL/UNAVAILABLE/UNTESTED状态。
ETF份额重点核查历史而非只有当日；基金季报重仓与完整持仓分开；指数权重/行业分类保留历史版本。相同原始上游不算独立验证源。
尊重限速与访问权限；不绕过验证码；失败后退避并告警。免费源不够时标记缺口或从启用日积累，不暗中替代、不自动购买。

P0交付：数据能力矩阵、可复现探测脚本、原始样本、缺口报告、推荐首批资产名单、历史回补/日常更新方案。根据结果确定实际覆盖与工期。

## 4. 采集契约

Adapter职责：discover、fetch、parse、validate；返回batch_id、source_id、upstream_id、records、artifacts、warnings，不直接写估算库。
source_id表示入口，upstream_id标识原始来源。
原始工件：artifact_id、content_hash、路径、抓取时间、URL（去除密钥）、内容类型、解析器版本。
任务状态：pending/running/succeeded/partial/failed；保存错误分类、重试次数、游标、数据区间。
按来源并发限制；指数退避加抖动；字段缺失、单位突变、空表触发隔离而非成功入库。任务使用唯一业务键及运行锁防止重复执行。
每天按来源实际发布时间安排采集；延迟披露定时回补，次日及后续检查修订。周频模型仅在输入快照完整度达到门槛后运行。

## 5. 身份、资产与去重

Entity：机构/主体；Vehicle：基金组合、ETF等；ShareClass：同一组合的份额类别；Asset：股票、指数等；Classification：行业/风格/主题；Channel：融资方式。
A/C份额归并组合，收益选择规范化代表序列，规模按不重复口径汇总。机构持有ETF和ETF底层资产是所有权/穿透两个视角，禁止相加。
股票行业映射带effective_from/to、classification_version。主题允许重叠但禁止汇总成互斥总量。宽基/行业/风格不可直接相加。
ETF净申赎不识别出资人；已披露政策相关主体的ETF持仓不等于今日申购归属于该主体。国家队异动只能独立标注关联观察，未经证据不得给确认买入额。

## 6. 存储设计

核心表：source_registry、raw_artifact、entity、vehicle、share_class、asset、asset_membership、fact_observation、position_disclosure、input_snapshot、model_run、scenario_definition、estimate、interpretation、validation_result、job_run。

fact_observation字段：id/source_id/artifact_id/subject_id/asset_id/metric/value/unit/currency/effective_at/published_at/ingested_at/revision_id/supersedes_id/quality_status。
缺失发布时间不可伪造，保存NULL及时间精度，相关数据不得进入严格公开信息回放。
position_disclosure额外字段：report_period、quantity、market_value、weight、disclosure_scope（top_holdings/full_portfolio/threshold_list）、company_action_basis。
历史修订append-only；最新视图按修订链查询。

input_snapshot：输入记录及修订ID清单哈希、as_of、coverage、分类版本。
model_run：代码提交、依赖锁版本、输入快照、参数、种子、模型版本、训练窗口、状态。
estimate：run_id/subject/asset/metric/value/lower/upper/unit/scenario_id/target_population/coverage/range_type/assumptions/quality/sensitivity/invalidation_conditions。
range_type：statistical/scenario_envelope/identification_bound；无校准依据不能输出概率。
interpretation：run_id/as_of/model_provider/model_name/prompt_version/content/evidence_refs/context_refs/validation_status。

两个历史查询模式：公开信息回放按published_at；真实运行回放额外按ingested_at。任何回测均禁止使用未来公布的披露或修订。

## 7. 计算引擎

### 7.1 ETF
折算调整后的份额S、同口径净值NAV：净申赎估算F≈ΔS×NAV_t。
规模变化分解：ΔA=ΔS×NAV_t+S_(t-1)×ΔNAV，显式记录估值残差/来源误差。
输出1/5/20/60交易日净申赎、份额增长、规模及底层暴露变化。实物申赎不等于新现金买股。
穿透使用当时有效权重；缺权重/陈旧权重降低覆盖，不能用今日权重重写历史。

### 7.2 两融
余额、买入、偿还、变化、行业集中度、相对流通市值/成交额比例。与ETF等并列，不相加成总净流入。期货不列首版核心，未来区分多空名义敞口与保证金。

### 7.3 披露持仓
优先比较公司行动调整后的股数；分离价格漂移。进入/退出阈值名单不解释为新建/清仓。归一主体名称，保留原始名称和映射证据。

### 7.4 主动公募周频Nowcast
先总体权益暴露，再权益内部行业分布；行业权重与风格载荷分开，避免共线性导致重复仓位。
收益分解r_f≈Σw_k r_k+ε。目标函数：收益拟合误差+披露先验偏离惩罚+周度权重变化惩罚。
约束：非负、组合类型允许的权益范围、合理总权重与变化范围。债券/现金/港股按基金类型加入，不能解释的暴露留unknown。
以价格漂移后的持仓为延续基线；将估计变化与被动权重变化分离。分红用总收益净值序列处理；规模只能用当时可得披露或独立估计。
披露先验覆盖部分时，不将未披露行业设为零。高共线行业先合并为宽行业组。
至少比较持仓延续基线与约束回归；动态模型仅在可验证改进后加入。

### 7.5 扩散与结构
方向超过预先规定的误差/显著性阈值才计增加或减少。另列不变与不可识别。
输出等权、规模加权扩散及各自分母、样本规模、覆盖率、基金家族集中度。样本间相关性用分组验证/自助法处理，不假设独立。
稳定性先做多维画像：配置持续、集中、申赎波动、杠杆参与、渠道一致。若未来加综合分，公开权重与敏感性，不称客观稳定概率。

## 8. 情景引擎

L1谨慎：有限持仓延续、样本内估计、宽区间；L2基准：中等换手、约束调仓、适度持仓保留；L3扩展：分组外推与更宽换手/同向性假设。
每档由版本化JSON/YAML配置驱动，记录参数、支持证据、适用基金类型与失效条件。三档不是多空档，也不要求数值单调。
L3提供弱/中同向等参数网格的敏感性范围，不默认所有未覆盖资金与样本一致。证据不足时允许不生成某档。
对比页必须同目标同单位；外推扩大目标总体时单独标注，不能与样本总金额无说明比较。

## 9. 成本引擎

重点：近20/60日新增配置批次的指数等效成本，不声称机构真实成本。
资金估计分拆为情景下gross_buy/gross_sell，不能把net_change直接等同买入。
批次字段：起始日、投入成本B、参考价格P、等效份额q=B/P、剩余份额、剩余成本、来源、情景。
剩余平均成本=Σ剩余成本/Σ剩余等效份额。卖出按比例/FIFO等规则做敏感性比较，不暗示哪种为真实账务。
L1可靠新增窗口宽参考带；L2中等换手和比例减持；L3多种买卖路径、保留率、样本外配置形成包络。
历史未知成本单列unknown批次。成本参考统一分红/成分/复权口径；输出指数等效成本距离，并标记非实际成交点位。
严禁把不同风格指数点位直接平均；跨资产汇总需共同归一参考序列，未满足则分别显示。
验证：账本守恒、合成交易路径恢复、假设敏感性；真实成本标签不足时明确不能验真。

## 10. AI报告契约

输入：结构化事实、三档估算、数据质量、宏观产业估值上下文及证据ID。
输出JSON：facts、L1、L2、L3、consensus、disagreement、cost_notes、risks、assumptions、invalidation、next_observations、evidence_refs。
数字引用程序核对；方向/日期/单位检查；禁止自造概率及估算值。引用不到证据的段落退回或剔除。来源文本视为不可信输入，不能执行其中指令。
LLM故障返回模板报告，采集和模型照常运行。展示仅辅助结构判断，不输出必涨必跌或自动交易指令。

## 11. API与界面

GET /api/v1/market/overview
GET /api/v1/sectors/ranking
GET /api/v1/sectors/{id}/structure
GET /api/v1/sectors/{id}/cost
GET /api/v1/scenarios/compare
GET /api/v1/evidence/{id}
GET /api/v1/quality/status

参数：as_of/window/scenario/classification_version/replay_mode。响应统一含data、as_of、published_cutoff、coverage、freshness、run_id、warnings、evidence_refs。
事实/估计/解释分区；三档并列；所有数字可追溯。四页：大盘总览、板块对比、板块详情、数据模型质量。首版用配置变化矩阵，不用暗示资金守恒迁移的桑基图。

## 12. 验证与发布门槛

数据：幂等、单位、公司行动、缺失、修订、时间回放、原始来源追溯、份额类别去重。
模型：时间滚动留出验证、禁止未来信息、与持仓延续基线比较。披露同一报告期末作为延迟验证标签；完整披露与部分披露分开打分。
指标：暴露MAE、方向一致率、统计区间覆盖率与宽度、模型失败率、样本覆盖。阈值在P0/研究初期预注册，不用测试集调参。表现不优于基线的模型保留research状态。
情景：同事实快照、配置可重现、分母清晰、区间类型正确、强假设撤销后结论撤销。
工程：任务重入、来源故障、时区、节假日、备份恢复、API契约、前端E2E、AI数字引用检查。
质量状态：valid/degraded/stale/unidentifiable/failed；stale须保留原as_of，不伪装今日结果。

## 13. 目录与运行

capital-observer/
  ingestion/ normalization/ storage/ models/ scenarios/
  research/ context/ analyst/ api/ dashboard/ jobs/
  tests/ docs/ configs/

独立服务账户与数据目录；配置密钥不入git；只读API与管理任务权限隔离；资源并发上限；日志去密；依赖锁定。
数据库备份、原始工件校验、可恢复性演练。无需修改已有游戏、论文、桥接服务。

## 14. 分期交付

P0 数据验证：来源能力矩阵、样本、缺口、预算（优先零付费）、覆盖建议。
P1 事实底座：原始归档、时间/修订、标准化、质量、可回放。
P2 观测版：ETF/两融/披露、基础看板、证据下钻。
P3 估计版：公募暴露、扩散、L1/L2/L3、留出验证报告。
P4 成本解释版：批次成本、敏感性、AI与模板报告。
P5 试运行：连续交易日观测、断源和恢复测试、模型监测。
各阶段通过后再扩范围；P0前不承诺完整数据覆盖与准确度。

## 15. 下一步与授权边界

下一步应为P0免费源验证，但本次仅形成技术方案。没有新建远程仓库、安装依赖、运行抓取、启动模型或部署服务。
待P0确定：分类许可/历史覆盖、实际样本名单、模型阈值、更新时刻、成本参数、资源工期。若免费数据不足，先交付缺口与降级方案，再由用户决定是否补付费源。
