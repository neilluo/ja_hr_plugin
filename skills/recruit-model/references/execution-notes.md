# execution-notes — 执行纪律

## 不变量（违反即 bug）

1. 远端调用只走 `shared/notable.py`：token 缓存、401 自动刷新、429/5xx 指数退避。
2. 查重先行：简历 附件MD5→手机号（含批内）；岗位 job_id。重跑同目录必须 created=0。
3. 附件先传后写：uploadInfos→OSS PUT→cell，任一失败该条不写表（不留无附件记录）。
4. 写完必回读：`readback_missing` 非空即 exit 1。
5. 扫描件/图片进 needs_ocr 不入库，交 agent 视觉补录。
6. 读回值已归一（select→字符串），下游不要处理 {id,name}。

## 并发与限流

- 附件上传：`Notable.map_parallel` 5 线程（I/O 密集）；解析与记录写串行。
- 记录写每批 10 条；钉钉 OpenAPI 有 QPS 限制，不要自行加写并发。
- 429/500/503/文档初始化中 自动退避重试 3 次；OSS PUT 同。

## 失败处置

| 报告字段 | 处置 |
|---|---|
| readback_missing 非空 | 重跑同目录（幂等），仍失败看 error |
| failed（附件/解析） | 看 error 文本；附件类重跑可恢复 |
| skipped_dup | 正常，向用户说明即可 |
| needs_ocr | 按 resume-intake 的补录流程 |

## 环境

- Python ≥ 3.9，stdlib + shared/vendor（pypdf/olefile），零 pip 依赖。
- 外部二进制回退：pdftotext、textutil(macOS)。
- 验证：`python3 -m unittest discover -s tests`（15 用例，含 mock HTTP 传输）。

## 历史教训（勿重犯）

- 凭证只进 .secrets.json/环境变量；仓库 public。
- OpenAPI 用中文字段名做 key，别引入字段 ID 映射。
- 扫描件用"抽不出手机号/邮箱"判，不用文本长度。
- 解析器 list 字段（certificates）入口 join，_cast 不做 str(list)。
- mock HTTP handler 必须先读 Content-Length body；测试模块别漏 import。
