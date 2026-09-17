# 简历/JD 原文提取方法（极速版）

**极速版里 agent 不再手写解析代码、也不再 `pip install` 任何东西。**文本提取与字段预抽全部由脚本内部完成（`shared/extract_text.py` + `shared/extract_fields.py`，零第三方 pip 依赖，olefile 与 pypdf 已 vendor 进 `shared/vendor/`）。本文件说明脚本的提取机制与各状态的业务口径，供 agent 转述失败原因、以及维护者排查用。

> 运行时唯一前提：机器上存在 Python 3（3.9~3.14）。Windows 用 `py -3`，`python` 别名可能静默失败（退出码 49）。

## 提取梯队（按文件类型）

| 类型 | 机制 | 兜底 |
|---|---|---|
| .docx | 标准库 zipfile 读 `word/document.xml`，按 `w:p` 段落拼接 `w:t`（表格版简历只取 `w:t` 会丢换行） | 报错转人工 |
| .doc（含 WPS 生成的复合文档） | vendor 的 olefile 读 `WordDocument` 流，fcMin/fcMac 偏移 0x18/0x1C（little-endian uint32），UTF-16-LE 解码并清洗控制符 | 报错转人工 |
| .pdf | **vendor pypdf → macOS JXA/PDFKit → macOS Vision OCR（扫描件救回，见下）→ agent 多模态兜底（P4a，见下）**（D9 梯队 + P3 Tier 1.5 + P4a）。禁止依赖 pdfplumber（其依赖链含 cryptography 二进制 wheel，客户机最容易装挂） | 无文本层且 OCR 不可用/不可信 → `needs_agent_vision`（转 agent 兜底，不再直接判死） |
| 图片（png/jpg 等） | **macOS 上自动 Vision OCR 入库**（P3，见下）；OCR 不可用/不可信 → agent 多模态兜底（P4a，见下） | `needs_agent_vision`（跨平台兜底已实现：agent 一轮多模态读完产出补丁） |

## Vision OCR 梯队（P3，仅 macOS，backend=vision_ocr）

- 机制：系统自带 `osascript -l JavaScript`（JXA）调 Vision framework 的 `VNRecognizeTextRequest`（Accurate 档，zh-Hans/zh-Hant/en-US，语言纠错开）。PDF 逐页 2x 缩放渲染后 OCR；图片直接 OCR。**零 pip 依赖、零联网、零 API key**，简历不出机器。实现在 `shared/extraction/vision_ext.py`。
- 触发条件：darwin 且（图片 或 前序文本层梯队全部没拿到可用文本）。**「拿到文本但护栏判定是水印/扫描件」的假 ok 也会被 chain 的 gate（`detect_scanned`）降级后落到本梯队**——这是扫描件 PDF（pypdf 能提出水印字符）能被救回的关键。
- OCR 结果**必须再过护栏**，不可信判 `no_text_layer` 而非假成功：①数字字符数为 0 直接不可信（简历几乎必含手机号/年份）；②`detect_scanned` 同款水印/重复串判定。实测 3 份失败件（1 png + 2 扫描 PDF）全救回、手机号 3/3、学历/学校/专业 3/3、姓名 2/3（png 那份姓名误抓但自动带「请人工确认」警告）。
- 性能：单份 1.16~1.58s；入库脚本提取阶段并发 ≤4（实测 3 份并行 1.764s vs 串行 3.810s）。
- **TCC 授权**：首次运行 osascript 读 ~/Desktop、~/Documents、iCloud 目录文件时 macOS 可能弹授权弹窗，需用户点一次允许（对已授权终端一般不弹）。弹窗挂起时梯队按 60s 超时转失败，重跑即可。
- notes/warning 会带 OCR 置信度摘要（行数、mean/min 置信度、低置信行数）与「OCR 文本可能有小误读」标记，agent 转述时保留。
- `RECRUIT_NO_VISION=1`（**仅测试用**环境变量）：Vision 梯队恒不受理，用于在 macOS 上演练下面的 agent 多模态兜底通道；生产流程绝不设置。

## agent 多模态兜底通道（P4a，跨平台，backend=agent_vision）

客户硬需求「不能接受简历解析报错」的最后一环：Vision OCR 只在 macOS 可用，非 macOS、或 Vision 失败/文本不可信时，提取终态仍是 `no_text_layer`——P4a 起这种文件**不再判死**，转成 agent 推理：

1. 入库脚本把这些文件记 `parse_status="needs_agent_vision"`，stdout 打印一行 `VISION_NEEDED: <绝对路径1> <绝对路径2> ...`（单行、空格分隔；清单同时进 `intake_report.json` 的 `vision_needed_files`），其余文件照常入库。
2. agent **一轮**用多模态能力读完全部列出文件，按补丁 schema（与 HOTPATH.md 逐字一致）Write 补丁 json：
   `{"<文件绝对路径>": {"text": "...", "fields_draft": {"name": "...", "phone": "...", ...}, "confidence": 0.0, "notes": "..."}}`
3. **重跑同一条命令**加 `--apply-vision-patch <补丁.json>`。合并规则（`shared/fields/merger.py` FieldMerger）：先对 `patch.text` 跑 RegexFieldExtractor；**regex 有值的字段用 regex**；regex 为空的字段才取 `fields_draft`；凡取自草稿的字段打 `field_source="agent_vision"` 并追加进该候选人 `needs_review`（回合 2 用 evidence 原文复核）；`patch.text` 写入简历全文字段并记 `backend="agent_vision"`。之后按正常候选走查重/写库/回读。
4. **agent 只产出结构化补丁，绝不写库**；补丁没覆盖的文件维持失败清单语义（如实告知，不硬造）。
5. **20% 闸门（用户拍板）**：`needs_agent_vision` 份数 / 总份数 > 0.20 → 疑似整批格式问题，**不写任何记录**，stdout 业务话「本批 X/Y 份读不出文字，超过 20% 阈值，疑似整批格式问题，请确认后重试或提供文字版」+ 名单，退出码 0、报告 `ok=true`、`partial=true`、`reason="vision_gate"`。agent 转述给用户确认，**不**打补丁。

## 提取状态（parse_status）与 agent 的业务话

| 状态 | 含义 | agent 必须怎么做 |
|---|---|---|
| `ok` | 提取成功（含 macOS Vision OCR 救回的扫描件/图片 backend=vision_ocr，与 agent 补丁救回的 backend=agent_vision） | 正常进判定；救回件的「可能有小误读」与人工确认警告照转；agent_vision 草稿字段按 `needs_review` 复核 |
| `needs_agent_vision` | 本机读不出文字（提取终态 `no_text_layer`：非 macOS，或 OCR 文本不可信——水印/重复串/0 数字字符），且本轮补丁未覆盖 | 走 agent 多模态兜底协议（见上节）：一轮读完 `VISION_NEEDED:` 清单 → 写补丁 json → 重跑加 `--apply-vision-patch`。触发 20% 闸门时改为请用户确认整批格式问题 |
| `no_text_layer` | 提取层原始终态（extract_text 层面）；入库脚本 P4a 起把它转成 `needs_agent_vision`，候选人层面不再出现 | —（维护者参考） |
| `garbled` | 提取出文本但乱码 | 进 ❌ 清单：建议重新导出标准 Word/PDF |
| `encrypted` | WPS/Office 加密 | 告知"文件被加密，请提供未加密版本"，不猜测内容 |
| `unsupported` / `error` | 格式不支持/读取失败 | 如实告知格式与失败原因 |

P4a 起失败清单语义收窄为「**仅加密/损坏/补丁未覆盖（或补丁后仍读不出手机号）才失败**」。判定扫描件：字符数阈值 + 水印串重复占比 + 数字字符数（`detect_scanned`）。本机提不出来先转 agent 兜底，兜底也读不出就停，**绝不猜测编造**。

## 字段预抽（正则，脚本完成）

脚本从纯文本预抽：姓名（正文优先、文件名兜底并标 `name_source`）、手机号、邮箱、最高学历、院校、院校排名、专业、期望职位/地点/薪资、证书、技能，以及教育/工作/技能/证书四个**原文段**（`sections`，判定回合的 evidence 来源，D4：教育/专业/证书段必须保留全文，只截断工作经历正文）。

**工作年限三档来源（D13，重点）**：

- `text`：正文明确写了年限 → 可直接用于硬门槛；
- `filename`：从文件名抽的（如"胡裕_14年.pdf"）→ 低置信，报告里有 warning；
- `estimated`：正文没写，按工作日期区间/毕业年份**估算** → digest 会标 `needs_review:["years"]`，**agent 必须在批量判定回合用 evidence 原文复核**并在 `candidate_overrides` 回填修正值；估算值直接喂"经验年限一票否决"风险高，复核不了就不要拿它当否决依据。

实测 28 份简历来源分布：正文 16 / 文件名 3 / 估算 9。

## 文件 MD5 与库内附件查重（P4b 起：一套真 MD5 打通三处）

提取时同步算文件**真 MD5**，它用于三处：本批内部去重（同一个文件传两次）、`checkpoint.json` 幂等续跑（重跑跳过已成功项）、以及**库内附件内容级比对**。

库内比对的键不再是 `(附件文件名, 附件字节大小)`。钉钉附件读回不带内容哈希，把库内附件全下载回来重算又贵（预签名 url 只有 2h 时效），所以改成**把哈希存进库里**：简历库多一个 text 字段「附件内容MD5」(`attach_md5`)，附件上传成功后由脚本把本地文件真 MD5 写进去；入库前扫库建「哈希 → record_id」索引，一次查询同时读 `attachment` 与该字段（**不增加 dws 调用次数**）。判定三层：

1. 本地 MD5 命中库内哈希 → 判重复上传跳过，可以说"库内已存在内容相同的简历附件（附件内容MD5 比对命中）"——**换了文件名也命中**（修掉老键的漏判：同一份内容改名重投不再重复建档）；
2. 未命中但 (文件名, 大小) 命中且库内那条有哈希 → 内容确实不同 → **不判重复**，按同一候选人的简历新版本走覆盖更新（new/overwrite 由手机号查重决定），清单说明"同名同大小但内容不同"（修掉老键的误判：改一版重投不再被静默跳过）；
3. 命中的库内记录**没有**哈希（P4b 之前写入的老记录）→ 无从比内容，按老键回退判重复 + 告警；该记录被覆盖更新（手机号命中）或补传附件时**自动回填**哈希（懒回填），库随之收敛到内容级去重。

**老库容忍**：config.json 的 `fields.resume.attach_md5` 缺失（客户现存库没建该列）→ 整库回退 `(文件名, 字节大小)`，并给一条 warning 说明"未启用内容级去重、误判/漏判风险、如何启用"；脚本**不崩溃、不自建字段**（建字段属复刻部署阶段）。此时话术必须回到"库内已存在同名同大小的简历附件（文件名+字节大小 比对命中）"，**不要**说成"MD5 相同/内容完全相同"——键是什么就说什么。

## 维护参考：.doc 提取核心逻辑（脚本内部实现，勿在对话中展示）

```python
import olefile, struct, re
o = olefile.OleFileIO(path)
wd = o.openstream('WordDocument').read()
fcMin, fcMac = struct.unpack('<II', wd[0x18:0x20])
txt = wd[fcMin:fcMac].decode('utf-16-le', errors='ignore')
txt = txt.replace('\x07', '\n').replace('\r', '\n')
txt = re.sub(r'[\x00-\x06\x08-\x1f]', '', txt)
txt = re.sub(r'\n{2,}', '\n', txt)
```

坑：`file` 显示 `Composite Document File V2, ... WPS Office` 时同样是该格式；**不要先复制改名**（会导致附件丢失原始文件名）。判定 doc/docx：Zip 容器 → docx；Composite Document File → doc。
