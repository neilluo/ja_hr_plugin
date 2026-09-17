# -*- coding: utf-8 -*-
"""match 包：match-verify 三脚本的 OO 分解（P8/P9，任务 #19）。

    constants.py    match 侧共享常量（build 侧 DEFAULT_MAX_PER_BATCH / JOB_STATUS_OPEN /
                    COMM_STATUS_ONBOARDED / DEFAULT_LOCATION；P9a 并入 apply/verify 侧
                    MATCH_SOURCE_* / FILTER_VALUE_CHUNK / EVIDENCE_MAX_LEN / GATE_ITEMS /
                    RECOMMEND_VALUES / INVALID_PASSED_RATIO_LIMIT）
    tablevalues.py  表值/文本归一小工具 —— **build 侧语义**的 as_list/as_text/as_number/
                    clean_ws/clip/full/dedupe_keep_order/compact/est_tokens 与截断上限
                    （apply 侧同名函数**不同义**，禁止合并，见模块头注）
    applyvalues.py  表值/时间小工具 —— **apply 侧语义**的 as_text/as_list/join_list/
                    chunks/_now/_today（P9a 终裁：与 tablevalues 不合并，各自独立实现）
    scoring.py      SCORING_RULES 评分口径文案（内嵌 digest，逐字不动）+ ScoreCalculator
                    （D16 重算实现体；推荐档位判定由 verify 入口 _recommend_of 注入）
    jsonio.py       JSON 读写原语：dump_json_doc（indent=1 无 sort_keys，键序即字节）+
                    strip_md_fence + load_json（原 verify 侧围栏容错读取，P9a 统一）
    jobparse.py     JobRecordParser —— 岗位表行 → 契约 §3.3 jobs 元素（parse_job_record
                    冻结签名的实现体；W-A extract_job_fields 构造注入、可为 None；
                    P9a 起 apply 的无 digest 降级路径也经入口装配复用本类，脚本间零 import）
    gates.py        PrefilterAuditor + EMPTY_GATE/EDU_ORDINAL —— P5 组织预筛错杀的
                    机械复查（学历 ordinal / 年限数值 / 证书粗筛；专业跳过）
    candidates.py   CandidateNormalizer —— candidates.json 元素 → digest 元素
                    （D4 evidence / D13 years / D14 地点兜底 / P5 身份安全阀）
    source.py       MatchSourceGateway —— build 侧唯一持 AITable 的 IO 边界
                    （candidates.json / job 表 / resume 表；两条稳定排序铁律在此）
    chunking.py     ShardPlanner —— D3 分片 + L3 组织预筛 + L4 字段裁剪 + token 估算
    digest.py       DigestBuilder —— build_digest 的编排实现（阶段方法 + 实例属性黑板；
                    D7 失败早退产物与已知缺陷豁免的 3 键 meta 原样保留）
    reporting.py    MatchReporter —— report_and_emit 的实现体（stdout 冻结契约：
                    ARTIFACT:/SHARD:/ERROR:/WARN:/PREFILTER_SUSPICIOUS: 行协议）
    hitmap.py       HitMapper（norm_item/squash/dedupe_norm/map_hits_to_items 三条映射
                    规则）+ GateVerdictReader（gate_verdict）+ as_str_list —— verify 侧
                    归一化与命中比对（D16 集合校验的基础设施）
    coverage.py     CoverageChecker —— expected_pairs「同组织 + 在招」必须覆盖组合枚举
    guardrails.py   SemanticGuardrails + SEM_GUARD_DEFAULTS —— 缺陷2 语义护栏
                    （SEM-E/H/R/G/O/P 六族；9 阈值键全部可传）
    verifier.py     DecisionVerifier —— verify() 冻结签名的实现体（err/warn 闭包 →
                    实例方法；四阶段拆分）+ build_result/dist/slim_for_stdout 结果原语
    decisionctx.py  DecisionContextBuilder —— job/candidate 索引 + 无 digest 时的表侧
                    降级合成（synth_digest_from_table 就地改写 job_key 的现状原样保留）
    overrides.py    OverrideMerger + OVERRIDE_KEYS/OVERRIDE_WRITEBACK_MAP —— 契约
                    v3 §9#1 七键修正合入候选人档案
    recordfactory.py MatchRecordFactory —— gate_text/cells_for/years_text/make_record
                    （写库 payload 的字段渲染；键插入序与 None 剔除规则冻结）
    matchgate.py    MatchTableGateway —— apply 侧唯一持 AITable 的 IO 边界（resume 写回 /
                    删旧建新 / D15 统计重算回填 / 双回读；dws 调用序列是 ARGV 指纹面）
    applyreport.py  ApplyReportBuilder —— rows/match_rows/summary 用户可读清单 +
                    stdout 的 printable_table
    applyflow.py    ApplyOrchestrator —— apply() 冻结签名的实现体（原 420 行上帝函数
                    的 8 步阶段方法 + 实例属性黑板；warnings 单一 sink 别名保留）

P8 第一刀动 build_match_input.py 一侧；P9a 第二刀动 apply_decisions.py /
verify_decisions.py 两侧：两入口只剩 CLI 装配 + 冻结签名薄壳（build_digest /
report_and_emit / parse_job_record / emit_shard_stdout / apply / verify / load_json /
norm_item / compute_scores 等）+ 三个裁判篡改定位锚点（build 入口 REQUIREMENTS_LIMIT、
apply 入口 CREATE_CHUNK、verify 入口 _recommend_of 的档位判定行——各自唯一且行为支配，
值经构造注入实现体）。intake_job.py 归 P9b，不在本包。

沿 P2/P7 纪律：**本包 `__init__` 不做任何 re-export**（不留 shim，调用方直接 import 子模块）。
import 风格与 shared 既有包一致：shared/ 在 sys.path 上，包内模块用顶层绝对 import。
"""
