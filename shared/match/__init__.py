# -*- coding: utf-8 -*-
"""match 包：match-verify 三脚本的 OO 分解（P8/P9，任务 #19）。

    constants.py    match 侧共享常量（DEFAULT_MAX_PER_BATCH / JOB_STATUS_OPEN /
                    COMM_STATUS_ONBOARDED / DEFAULT_LOCATION；目前只收 build 侧定义）
    tablevalues.py  表值/文本归一小工具 —— **build 侧语义**的 as_list/as_text/as_number/
                    clean_ws/clip/full/dedupe_keep_order/compact/est_tokens 与截断上限
                    （apply 侧同名函数**不同义**，禁止合并，见模块头注）
    scoring.py      SCORING_RULES 评分口径文案（内嵌 digest；P9 抽 ScoreCalculator 归此）
    jsonio.py       digest 文档落盘原语（indent=1 无 sort_keys，键序即字节）+ 围栏剥离
    jobparse.py     JobRecordParser —— 岗位表行 → 契约 §3.3 jobs 元素（parse_job_record
                    冻结签名的实现体；W-A extract_job_fields 构造注入、可为 None）
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

P8 第一刀只动 build_match_input.py 一侧：`skills/match-verify/scripts/build_match_input.py`
只剩 CLI 装配 + 冻结签名薄壳（build_digest / report_and_emit / parse_job_record /
emit_shard_stdout / main）。apply_decisions.py / verify_decisions.py 的 OO 化归 P9，
届时按分析报告 D.2/D.3 扩本包（ScoreCalculator → scoring.py、TextNormalizer 等），
**同名不同义**的 apply 侧 as_list/as_text 逐案裁定前不得并入 tablevalues.py。

沿 P2/P7 纪律：**本包 `__init__` 不做任何 re-export**（不留 shim，调用方直接 import 子模块）。
import 风格与 shared 既有包一致：shared/ 在 sys.path 上，包内模块用顶层绝对 import。
"""
