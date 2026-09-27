# execution-notes — 执行纪律

## 不变量（违反即 bug）

1. 远端调用只走 `shared/notable.py`：token 缓存、401 自动刷新、429/5xx 指数退避。
2. 查重先行：简历 附件MD5→手机号（含批内）；岗位 job_id。重跑同目录必须 created=0。
3. 附件先传后写：uploadInfos→OSS PUT→cell，任一失败该条不写表（不留无附件记录）。
4. 写完必回读：`readback_missing` 非空即 exit 1。
5. 扫描件/图片进 needs_ocr 不入库，交 agent 视觉补录。
6. 读回值已归一（select→字符串），下游不要处理 {id,name}。
7. 匹配只有一条主链路：`match_gated.py`（机械门槛+语义打分）→ `match_analyze.py`
   prepare/merge/apply/stats（逐岗 subagent 判定并落库）。`--commit` 是无 subagent 的兜底
   直连落库，二者不要混跑；统计必须另起一次读取（同脚本内写完立刻回读拿到索引前旧值）。
8. 阈值/枚举/词表单一真源：推荐阈值与 MIN_SCORE 见 `match_gated.py`，recommend/source 等
   选项见 `config.json options`，技能词表与分词见 `shared/vocab.py`，
   同义/上下位词典见 `skills/match-verify/scripts/semantic_score.py`。文档与脚本都不得再抄副本。
9. subagent 并发一律经 `shared/waves.py` 规划：agent 数硬上限 `MAX_AGENTS=20`（只能下调），
   batch=ceil(条数/agent数) 自动均衡，一次性并发发出，不分波、不串行。

## 并发与限流

- 附件上传：`Notable.map_parallel` 5 线程（I/O 密集）；解析与记录写串行。
- 记录写每批 10 条；`Notable.call()` 全局 pacing 20 req/s，不要自行加写并发。
- 429/500/503/文档初始化中 自动退避重试；401 自动刷 token 重试一次；OSS PUT 同纪律。
  QPS 403（QpsLimitForApi/QpsLimitForAppkeyAndApi）是网关级拒绝、请求未被处理，可重试且
  不受 idempotent 门禁约束；stage-0 含整点峰值规避（整点±10s 内等待至整点+10s）。
- subagent 并发（skills-analyze / job-intake / match-verify 三处）一律经 `shared/waves.py`
  规划，硬上限 20 个 agent，一次性并发发出。

## 失败处置

| 报告字段 | 处置 |
|---|---|
| readback_missing 非空 | 重跑同目录（幂等），仍失败看 error |
| failed（附件/解析） | 看 error 文本；附件类重跑可恢复 |
| skipped_dup | 正常，向用户说明即可 |
| needs_ocr | 按 resume-intake 的补录流程 |

## 环境

- Python ≥ 3.9，stdlib + shared/vendor（pypdf/olefile/typing_extensions），零 pip 依赖。
- 外部二进制回退：pdftotext、textutil(macOS)。
- 验证：`python3 -m unittest discover -s tests`（用例数以实跑输出为准，别在文档里写死数字）；
  改动后另跑 `python3 -m py_compile shared/*.py skills/*/scripts/*.py`。
- 写脚本前跑 stage 0 预检：`bash shared/preflight/preflight.sh`（Windows 用
  `shared/preflight/preflight.ps1`）；业务入口脚本内部已调 `run_preflight`。

## 历史教训（勿重犯）

- 凭证只进 .secrets.json/环境变量；仓库 public。
- OpenAPI 用中文字段名做 key，别引入字段 ID 映射。
- 扫描件用"抽不出手机号/邮箱"判，不用文本长度。
- 解析器 list 字段（certificates）入口 join，_cast 不做 str(list)。
- mock HTTP handler 必须先读 Content-Length body；测试模块别漏 import。
