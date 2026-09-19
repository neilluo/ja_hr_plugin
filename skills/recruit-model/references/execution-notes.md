# 执行注意事项与踩坑清单（极速版）

来源：老版真实部署迭代验证 + 极速版流水线实测。所有入库/匹配/查询类技能执行前遵守本文件。

## 沟通铁律（与老版完全一致）

- **只说业务语言**：对用户只讲「岗位 / 候选人 / 匹配结果 / 推荐状态」，绝不出现表ID、字段ID、fieldId、命令、JSON、脚本路径、产物文件名。
- 覆盖、删除前用业务话讲清"对谁做什么、后果"，得到用户明确指令后执行；用户说"上传并解析"即视为入库授权。
- 汇报用清单/表格，一张表只说一件事（新岗位 / 新简历 / 匹配结果 / 未达标），不堆砌全部字段。
- 解析失败（加密/损坏/乱码）如实告知并建议提供文字版，**绝不硬造字段**；失败项不隐藏。P3 起 macOS 上扫描件/图片简历由脚本自动走系统 Vision OCR 救回入库（首次运行可能弹 macOS 授权弹窗，请用户点允许）。**P4a 起 OCR 不可用/不可信（非 macOS 等）的文件不再判死，转 agent 多模态兜底**：脚本 stdout 打印一行 `VISION_NEEDED: <绝对路径...>`（清单同进报告 `vision_needed_files`，entry `parse_status=needs_agent_vision`）→ agent **一轮**多模态读完全部列出文件 → 按 schema（`{"<文件绝对路径>": {"text": "...", "fields_draft": {...}, "confidence": 0.0, "notes": "..."}}`）Write 补丁 json → 重跑同命令加 `--apply-vision-patch <json>`。合并规则：先对 patch.text 跑正则，**regex 有值用 regex、为空才取 fields_draft**；取自草稿的字段打 `field_source=agent_vision` 并进该候选人 `needs_review`（回合 2 用 evidence 原文复核）。**agent 只产出补丁、绝不写库**；补丁未覆盖的文件维持失败清单语义。**20% 闸门（用户拍板）**：本批 ≥5 份且 `needs_agent_vision` 份数/总份数 >0.20 → 本轮不写任何记录，退出码 0、`ok=true`、`partial=true`、`reason=vision_gate`。闸门只拦本轮写入、不拦补救：`VISION_NEEDED:` 照常打印，agent 仍一轮读完清单、Write 补丁 json、重跑同命令加 `--apply-vision-patch` 即可入库；补丁之后仍读不出的才是真正的「疑似整批格式问题」，那时再转述给用户请其提供文字版。`RECRUIT_NO_VISION=1` 仅测试用（关 Vision OCR 演练兜底通道），生产不设置。OCR/补丁救回件带「可能有小误读」警告，转述时保留。

## 三段式流水线纪律（性能的全部来源）

- 每批工作固定三个回合：**Turn 1 跑脚本 → Turn 2 agent 一次批量语义判定 → Turn 3 跑脚本**。
- **⛔ Turn 2 禁止规则脚本代跑（W-F run3 实测事故后新增，2026-09-17）**：简历侧 Turn 2（批量语义判定）与岗位侧 Turn 2（JD 语义归一化）**必须由 agent 自己（大模型）完成**，禁止编写或调用任何规则脚本/关键词匹配脚本（如自写 `generate_decisions.py`、`normalize_jobs.py`）代替语义判定。理由（实测）：run3 里 agent 用自写规则脚本跑两个 Turn 2，`verify_decisions` PASS、`apply_decisions`/`--apply` ok=true、**全部形式校验通过，但业务结果崩塌**——0 条推荐（run1/run2=22 条）、skill_hits 普遍 ≤3、evidence 同人同模板跨岗位复用、3 个岗位错归职能中心导致组合面改变。这是本架构最危险的失效模式：**静默产出看起来正常的错误结果**。`verify_decisions.py` 的 `sem_*` 语义护栏与 `intake_job.py --apply` 的「Turn 2 归一化护栏」只能**事后告警**，不能代替真判定；**护栏告警必须如实向用户呈现并说明可能需要重做判定，禁止静默吞掉告警继续写库**。
- **除 Turn 2 的语义判定外，禁止 agent 逐条敲 `dws` 命令。** 所有表格读写都发生在脚本内部（批量 ≤100 条/次、带重试；stage 6b 补传附件带写后回读，stage 7 批量 upsert 不做回读——record_id 从 upsert 响应直接提取）。老版逐条敲命令每回合边际墙钟成本约 5.4 秒（大模型推理 ~4s + 命令 ~1.3s），一份简历 20~40 回合——这是批量上传慢的根因，不许回退。
- 一次只调用一个脚本；同一批次 Turn 1 / Turn 3 使用同一个 `--out-dir`。
- 批量写入实测参考：31 条记录批量写 1.38s/1 次调用（逐条 38.79s/31 次）；批量查重 1.33s/1 次（逐条 39.50s/31 次）；附件并发 5 上传 2.79s（串行 5.59s）。

## 防静默早退：产物凭证校验（D7，每个脚本步骤必做，口径按契约 v3 §9#2 统一）

实测 agent 静默早退率约 25%，因此**每一步脚本跑完必须验产物，不许凭感觉进下一步**：

1. 脚本会在 stdout **末行**打印 `ARTIFACT:<绝对路径>`，这是产物凭证。
2. **统一口径：产物「文件存在 且 `ok == true`」才算过。**逐个产物：
   - `intake_report.json` / `apply_report.json`：顶层 `ok`；
   - `jobs_draft.json`：顶层 `ok`（与内嵌 `report.ok` 一致）；
   - `digest.json`（及分片 `digest_batch_NN.json`）：顶层 `ok`，`ok == false` 时看 `errors[]`（业务话原因，如"表里查不到在招岗位"）；
   - `decisions.json`（agent 产物，没有 ok 字段）：以 `verify_decisions.py` **退出码 0（校验结论 ok==true）**为凭证。
3. 文件不存在、或 `ok != true` → **重跑该步**（脚本按 checkpoint.json 幂等续跑，不会重放已成功项），最多重跑 2 次。
4. 仍失败 → 如实告知用户失败原因与已完成部分，**禁止跳过该步或假装成功**。
5. 报告里的 `rows[].result=失败` 与 `warnings` 必须转述给用户（业务话），禁止静默丢弃（D6）。

## 批量语义判定分片（D3，上限按契约 v3 §8 修订）

- Turn 2 判定输入按 `digest_batch_NN.json` 分片，**每片缺省 ≤8 人，一片一个回合**，禁止一次性吞下全部候选人。
- 用户**明确知情**时可上调 `--max-per-batch` 到 16（实测 16人×19岗=304 组合：226s/输出 33.5k token/2 次重试后成功，吞吐更高但重试率上升）；**禁止 >16**。
- 实测依据：10 人已逼近输出上限、20 人语义崩塌（漏人、编造命中项）、28 人撞顶不可恢复（稠密格式；稀疏 decisions 格式显著缓解输出压力）。
- 每片产出一个 `decisions_batch_NN.json`，全部片完成后合并为一个 `decisions.json`（`passed` / `rejected` / `candidate_overrides` 三个数组直接拼接，`batch_id` 保持一致），再走校验与应用。

## decisions.json 校验范围（以 verify_decisions.py 代码为准，契约 v3 §9#9）

**硬错误**（`ok=false`、退出码 1，整批不许写库）：

1. 文件不存在 / 不可读（`file_not_found` / `file_unreadable`）；
2. 非法 JSON（`bad_json`；带 markdown 围栏能剥掉解析成功的降级为 warning）；
3. digest / decisions 不是对象，或 digest 缺 `candidates` / `jobs` 数组；
4. `passed` / `rejected` 不是数组，或两者全缺（`decisions_empty`）；
5. 条目不是对象，或缺必需键：passed 的 `candidate_key`/`job_key`/`skill_hits`/`bonus_hits`/`gate_detail`、rejected 的 `candidate_key`/`job_keys`（`missing_field`/`job_keys_not_list`）；
6. 引用了 digest 里不存在的 `candidate_key`/`job_key`（`unknown_candidate_key`/`unknown_job_key`）；
7. **覆盖率缺失/重复**：每个 候选人×同组织在招岗位 组合必须在 passed 或 rejected 里出现且仅出现一次（`missing_pair`/`duplicate_pair`）；
8. passed 条目的 `gate_detail` 含不达标项（硬门槛一票否决却进了 passed）或不是对象（`gate_detail_inconsistent`/`gate_detail_not_object`）；
9. `evidence` 为空或超过 80 字（`evidence_empty`/`evidence_too_long`）；
10. `recommend` 取值不是 推荐/待定/不推荐（`recommend_invalid_value`）；
11. **编造命中项占比过高**：无法映射回岗位技能列表的 pass 条目 >34% → 判模型输出整体不可信，拒绝写库（`too_many_invalid_passed`）；
12. **evidence 跨候选人复用占比 >20%**（`sem_evidence_cross_candidate`，语义护栏，2026-09-17 新增）：evidence 必须是本人简历原文，同一段文本被多个候选人引用=编造，占比高时整批拒写库。

**语义合理性护栏**（`sem_*`，2026-09-17 新增；防 W-F run3 型「Turn 2 规则脚本代跑 → 形式校验全过但业务崩塌」；阈值在 `verify_decisions.SEM_GUARD_DEFAULTS`，可用 `--sem-*` CLI 覆盖；每条告警都带具体证据——哪个 candidate/job、实测值 vs 阈值）：evidence 去重率 <0.40（`sem_evidence_reuse`，run3 实测 0.33、正常批 0.55/0.67）、skill_hits ≤3 占比 ≥90% 且岗位分母中位数 >4（`sem_skill_hits_too_few`）、分母 ≥5 的条目 100% 全命中占比 ≥60%（`sem_skill_hits_full_inflated`，放水侧）、整批 0 推荐（`sem_zero_recommend`）或推荐率 >60%（`sem_recommend_ratio_high`）或推荐分布塌缩到单一取值（`sem_recommend_collapsed`）、门槛全拒（`sem_all_rejected`）/全过（`sem_all_passed`）、digest 标了 `needs_review:["years"]` 却一个 `years_experience` override 都没有（`sem_years_review_missing`，D13 复核没做）；**P5 安全阀复核落点**：`needs_review` 含 `"org"`（组织预筛机械复查命中 `prefilter_suspicious`）/`"name"`（姓名来源文件名/OCR/agent 草稿）/`"email"`（邮箱疑似 OCR 噪声）却无对应复核落点（org=`candidate_overrides.org`/`org_reason`；name/email=该人条目 evidence 里的复核说明）→ `sem_org_review_missing`/`sem_name_review_missing`/`sem_email_review_missing`（warning 强制人工复核，不拒写）。**护栏告警必须如实转述给用户并说明可能需要重做 Turn 2，禁止静默吞掉继续写库**；实测指标在 verify 输出 `summary.semantic_guards.metrics`。标定：W-F run3 退化样本触发 4 类护栏告警，run1/run2 正常样本 0 误报。

**告警**（不拦路，但必须业务话转述）：单条命中项越界判编造（该条 pass 无效、不建记录，`fabricated_skill_hit`/`fabricated_bonus_hit`）、命中项归一化映射成功（模型把切碎条目合回一句/缩写，`skill_hit_normalized`）、同集合重复命中已去重（`duplicate_hits`）、模型分数/推荐与脚本重算不一致（以脚本为准，D16，`score_mismatch`）、分母告警（岗位必备技能为空→技能得分记 0；加分项为空→加分得分记 100，`score_denominator`）、`gate_detail` 缺项、`batch_id` 与 digest 不一致、rejected 缺 `reason` 或 `job_keys` 为空、判定了跨组织/不在招组合（不会建记录）、没传 digest 的降级路径覆盖率未校验（`coverage_not_checked`）、上述全部 `sem_*` 语义护栏。

**candidate_overrides 只认七个键**（契约 v3 §9#1）：`org` / `category` / `expected_location` / `skills_extra` / `certificates_extra` / `years_experience` / `org_reason`；其余键忽略并进 warnings。修正在 Turn 3 由脚本写回简历库（emit/replay 模式下 stage 7 不做回读，record_id 从 upsert 响应提取）。

## 模型不输出分数（D16）

- Turn 2 只输出 `skill_hits` / `bonus_hits` / `recommend`（预判）/ `evidence`（原文引用）；**技能得分、加分项得分、匹配总分、最终推荐状态一律由脚本按口径重算**。
- 理由：实测模型算术自洽但会被 JD 分母粒度污染，且容易编造命中项——脚本重算并校验 `skill_hits ⊆ 必备技能`、`bonus_hits ⊆ 加分项`，越界条目作废进 warnings；同时省下大量输出 token。
- agent 预判的 `recommend` 与脚本按阈值重算结果不一致时，**以脚本为准**，不一致条数会记录在 warnings 里，转述给用户即可，不要争辩或手改分数。

## 跨平台调用（D10）

- macOS / Linux：`python3 scripts/xxx.py ...`
- Windows：`py -3 scripts\xxx.py ...`
- **Windows 上 `python` 别名可能静默失败（退出码 49，无任何输出）**——不要用裸 `python`。脚本退出码异常且无输出时，先换 `py -3` 重试。
- 脚本兼容 Python 3.9+，零第三方 pip 依赖（olefile、pypdf 已 vendor 进 `shared/vendor/`）；但**机器上必须存在 python 运行时**，千问办公不自带。缺失时引导用户安装后重跑。
- `--config` / `--files` / `--out-dir` 一律传**绝对路径**；SKILL.md 里的 `scripts/xxx.py` 是相对本技能目录的路径，调用前先按平台注入的技能基目录展开。

## 幂等与续跑（D12，checkpoint 语义按契约 v3 §9#6 加强；P3 起增量落盘 + 墙钟预算）

- 简历入库脚本落 `checkpoint.json`，**每个成功文件把「记录已写」（record_written）与「附件已传」（attachment_uploaded）分开记状态**（还含 md5/record_id/file_name/phone/org 等派生字段，跳过重跑时用于恢复 candidates.json 的完整性）。
- **增量落盘（P3）**：两个状态各自一确立就原子写盘（tmp + os.replace）——附件按上传分片逐片落、记录在 upsert 响应返回 record_id 后逐条落。进程被杀/撞工具超时不再丢全部进度。文件带 `version: 2` 与 `progress` 段（尚未写库文件的中间状态，只作断点可见性）；旧格式 checkpoint 照常可读，整个文件读不懂时视为空并告警，不崩溃。
- 重跑判定 = 记录已写 **且**（附件已传 **或** 本次带 `--no-attachment`）→ 整条跳过；记录已写但附件欠传且本次没带 `--no-attachment` → **只补传附件**（按 record_id 更新附件字段，绝不重复建记录），stage 6b 通过 `poll_fixup_attachments` 回读附件非空才算完成。**补传路径每轮最多处理 100 份（P4a 主控裁决）**：超出部分 defer 到下一轮（entry 记「未完成」、进 `deferred_attachment_files`、stdout 说明），重跑同一命令续补——防大批量补传把墙钟拖爆。
- 这是老版历史 bug「曾因批量路径跳过附件导致『单个有、批量空』」的防线：`--no-attachment` 跑完后，再次运行**不带**该参数必须能把附件补上（实测 28 份：补传 28/28、新建记录 0、第三轮重跑全跳过 0 次 dws 调用）。
- **墙钟预算 `--wall-budget <秒>`（P3，默认 100，必须小于 agent 工具 120s 超时）**：到点 graceful 停——落 checkpoint、打印已完成/未完成清单与一行 `RESUME:`（内容即原命令）、退出码 0、报告 `ok=true` 且 `partial=true`（带 `pending_files` 未完成名单与 `deferred_attachment_files` 附件欠传名单）。附件触顶停传后**记录仍照常批量 upsert**（欠附件的重跑走"只补附件"路径），保证每轮都有真实入库进度。**脚本内部不循环子批**；续跑 = 重跑同一条命令（checkpoint 幂等，不产生重复记录）。agent 纪律：**见 `RESUME:` 就重跑同命令，最多 3 次；仍 partial 才把已完成/未完成清单报给用户**。partial 时 `--auto-match` 自动跳过（candidates 不完整不进判定）。补传附件阶段（6b）不受预算门控（它是在完成上一轮已提交的记录），但受**每轮 100 份上限**约束（见上条）。
- **`turns_saved_estimate` 口径（P4a 主控裁决）**：只按**已完成**文件计（老插件 25 回合/份 × 已完成份数 − 本脚本 1 回合）；「未完成」文件（预算耗尽/闸门/补传 defer）本轮没做完，**不计入**省下回合——报告与 stdout 的估算值不再虚报。
- 岗位侧不落 checkpoint：幂等靠「岗位名称+所属部门+组织分类」复合键查表；`--no-attachment` 后重跑（不带该参数）会走覆盖更新路径重新上传并写入 JD 附件，同样不会重复建岗。
- 用户要求"从头重来"时才加 `--reset`（会清空断点——P3 起清空动作在清表前就先落盘，被杀也不会留下"表已清空但旧断点还在"的假状态；已写入表格的记录按查重主键覆盖更新，不会产生重复记录）。

## 批量与边界

- 库内附件比对的键（P4b 起）= **附件内容真 MD5**，存在简历库「附件内容MD5」(`attach_md5`) 列里（附件上传成功后由脚本写入）：内容相同（**换文件名也命中**）→ 判重复上传跳过，告知"库内已存在内容相同的简历附件（附件内容MD5 比对命中）"；同名同大小但内容不同 → **不判重复**，按同一候选人的简历新版本走覆盖更新（new/overwrite 由手机号查重决定），清单说明照转。**老库容忍**：客户现存库没有该列（config.json 缺 `fields.resume.attach_md5`）→ 自动回退「文件名 + 字节大小」并打一条 warning 说明未启用内容级去重与启用方法，脚本不崩溃、**不自建字段**；库内 P4b 之前写入的老记录（无哈希）同样按老键回退判重并告警，被覆盖更新/补传附件时**自动回填**哈希（懒回填），库随之收敛。本批内部去重与 `checkpoint.json` 幂等续跑用的是同一套真 MD5。
- **手机号冲突但姓名不同 → 疑似重名/录入错误，停止并请用户确认**，不自动选第一条（digest 中 `dedupe=="conflict"` 的候选人不进判定，先问）。
- JD 中部门不在选项内 → 脚本按"只增不删"补部门选项后再入库，并在报告中告知。
- 老版"≤5 个逐个串行、>5 个先说分批"的规则**作废**：极速版天然整批处理，31 份简历也是一个 Turn 1；仅 Turn 2 语义判定按 ≤8 人/片分回合。

## 选项维护（多选/单选字段）

- **组织新增**（如研发中心）：岗位表、简历库、匹配表三处组织选项都要同步添加，否则匹配不到。**添加方式 = 直接写记录时给选项名**（服务端自动补建，见下条），不要手工 `field update` 字段配置。
- **技能标签/部门/地点等新选项（2026-09-17 起根治口径）**：`shared/aitable/optionpool.py` 的 `OptionPool.ensure_options` 已改为**只读 + 延迟补建**——只 `field get` 读回现有选项池（供报告）并把缺失名记入 `pending_options`，**彻底移除了 `dws aitable field update` 路径**；实际补建由 `record create/update/upsert` 直接写**选项名**时服务端自动完成（自动补入缺失选项、**不动已有选项 id**，单字段选项上限 3000）。intake/apply 各脚本均已按此运行，agent 无需（也不允许）另行补选项。
- ⚠️ **为什么禁止 `field update` 改选项（生产数据丢失级事故，W-G 复现丢失、W-H 查明根因并受控实验验证修复）**：`field get` 对选项配置有**最终一致性**，可能返回任意陈旧的快照（W-H 实测：真实池 94 个选项时读回建表时的 7 个）；旧版 `ensure_options` 用这份快照构建「全量 payload」发 `field update`（整体覆盖语义）、又用**同一份快照**做读回校验——payload 漏选项/带上传播中的中间态 id 时，服务端重建选项、**重新分配 option id**（churn），而存量记录单元格按 option id 引用选项，id 一变旧引用悬空、值被**静默清空**（W-G 实测：一次追加选项把 27 条简历「技能标签」清掉大半，30→1、10→0、40→null；且旧校验对陈旧快照里没见到的选项**检不出丢失**）。是否触发全凭快照新鲜度=时序运气，这条路径永远无法安全。**唯一安全路径 = 永不触发 `field update`，新选项一律靠写记录时服务端自动补建**（W-H 受控实验：21 条存量记录 585 个多选值 + 21 个单选值，intake 追加 3 个新标签后**零丢失**、91 个已有 option id 零 churn、新标签全部进池且新记录标签集合逐一比对一致）。
- 若历史遗留原因必须动字段选项配置：先在表里备份受影响记录的全部选项值（导出），改完立即回读抽查老记录，丢失就从 intake 产物（candidates.json / jobs_draft.json）重写恢复。
- singleSelect/multipleSelect 写入一律用**选项名**（不是 id）；`field get` 读回的 id 只用于审计比对，不作为写入依赖；config.json 的 `options` 段仅是只读缓存。

## 附件

- 一律用**原始文件名**上传，**禁止改名/加序号前缀**，避免与库内附件比对失真。
- 附件上传由脚本并发执行（并发度 5，API 限 20 QPS 留余量）；无批量接口但并发后实测 5 个附件 2.79s。
- **批量入库 ≠ 省略字段**：批量也必须逐条带原始附件、逐条写权重（缺省 70%/30%）、提交时间等必填项——这些由脚本统一保证，agent 只需核对报告里 `attachment_failed` 与 `warnings`，有失败必转述。
- 用户说"先不传附件" → Turn 1 加 `--no-attachment`；之后"补传附件" → 重跑同一命令（不带 `--no-attachment`），checkpoint 的「记录已写/附件已传」分离判定保证**只补缺失的附件、不重放记录**（见「幂等与续跑」）。

## 清单式固定输出（每次操作后必出，单个文件也出清单）

- **上传处理清单**：逐文件一行，✅新入库 / ✅已覆盖 / ⏭️跳过 / ❌失败 + 业务可读原因 + 末尾小计（新入库 N | 覆盖 N | 跳过 N | 失败 N | 附件已传 N | 附件失败 N）。
- **匹配结果清单**：岗位｜匹配度｜技能｜加分｜结论；未过门槛的标注原因与岗数；失败项不隐藏。
- **批量汇总清单**：新入库简历 N 人/岗位 N 个、覆盖、产生匹配、待关注项（warnings 的业务话转述）。
- 数据源是脚本报告（`rows` / `summary` / `warnings`），agent 只做业务话转述与排版，**不改数字、不隐藏失败项、给下一步建议**。查询类同样以清单返回，一行一个候选人/岗位，末尾汇总。

## 写后回读

- 回读验证**仅在 stage 6b（补传附件）执行**——通过 `poll_fixup_attachments` 轮询附件非空才算完成。**stage 7（批量 upsert / 记录写入）在 emit/replay 模式下不做回读**，record_id 直接从 upsert 响应中提取，不发起单独的回读查询。
- agent 不只凭退出码宣称成功：**只认产物报告 `ok==true` 与 `rows` 里的逐行结果**；报告说失败就是失败，如实转述。

## 覆盖与破坏性操作

- 覆盖更新（手机号/岗位三元组命中）在报告里以"已覆盖"呈现并说明对象；用户说"上传并解析"即视为覆盖授权，但**删除类、重建全部匹配类破坏性操作必须先业务话确认**。
- 破坏性场景先加 `--dry-run` 看 apply_report 预演结果，经用户确认再去掉 `--dry-run` 实跑。
