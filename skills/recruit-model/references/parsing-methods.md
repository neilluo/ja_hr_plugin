# 简历/JD 原文提取方法（极速版）

**极速版里 agent 不再手写解析代码、也不再 `pip install` 任何东西。**文本提取与字段预抽全部由脚本内部完成（`shared/extract_text.py` + `shared/extract_fields.py`，零第三方 pip 依赖，olefile 与 pypdf 已 vendor 进 `shared/vendor/`）。本文件说明脚本的提取机制与各状态的业务口径，供 agent 转述失败原因、以及维护者排查用。

> 运行时唯一前提：机器上存在 Python 3（3.9~3.14）。Windows 用 `py -3`，`python` 别名可能静默失败（退出码 49）。

## 提取梯队（按文件类型）

| 类型 | 机制 | 兜底 |
|---|---|---|
| .docx | 标准库 zipfile 读 `word/document.xml`，按 `w:p` 段落拼接 `w:t`（表格版简历只取 `w:t` 会丢换行） | 报错转人工 |
| .doc（含 WPS 生成的复合文档） | vendor 的 olefile 读 `WordDocument` 流，fcMin/fcMac 偏移 0x18/0x1C（little-endian uint32），UTF-16-LE 解码并清洗控制符 | 报错转人工 |
| .pdf | **vendor pypdf → macOS JXA/PDFKit → macOS Vision OCR（扫描件救回，见下）→ 报错转人工**（D9 梯队 + P3 Tier 1.5）。禁止依赖 pdfplumber（其依赖链含 cryptography 二进制 wheel，客户机最容易装挂） | 无文本层且 OCR 不可用/不可信 → `no_text_layer` |
| 图片（png/jpg 等） | **macOS 上自动 Vision OCR 入库**（P3，见下）；非 macOS 无文本层可提 | `no_text_layer`（跨平台 OCR 兜底在后续版本规划中，当前未实现） |

## Vision OCR 梯队（P3，仅 macOS，backend=vision_ocr）

- 机制：系统自带 `osascript -l JavaScript`（JXA）调 Vision framework 的 `VNRecognizeTextRequest`（Accurate 档，zh-Hans/zh-Hant/en-US，语言纠错开）。PDF 逐页 2x 缩放渲染后 OCR；图片直接 OCR。**零 pip 依赖、零联网、零 API key**，简历不出机器。实现在 `shared/extraction/vision_ext.py`。
- 触发条件：darwin 且（图片 或 前序文本层梯队全部没拿到可用文本）。**「拿到文本但护栏判定是水印/扫描件」的假 ok 也会被 chain 的 gate（`detect_scanned`）降级后落到本梯队**——这是扫描件 PDF（pypdf 能提出水印字符）能被救回的关键。
- OCR 结果**必须再过护栏**，不可信判 `no_text_layer` 而非假成功：①数字字符数为 0 直接不可信（简历几乎必含手机号/年份）；②`detect_scanned` 同款水印/重复串判定。实测 3 份失败件（1 png + 2 扫描 PDF）全救回、手机号 3/3、学历/学校/专业 3/3、姓名 2/3（png 那份姓名误抓但自动带「请人工确认」警告）。
- 性能：单份 1.16~1.58s；入库脚本提取阶段并发 ≤4（实测 3 份并行 1.764s vs 串行 3.810s）。
- **TCC 授权**：首次运行 osascript 读 ~/Desktop、~/Documents、iCloud 目录文件时 macOS 可能弹授权弹窗，需用户点一次允许（对已授权终端一般不弹）。弹窗挂起时梯队按 60s 超时转失败，重跑即可。
- notes/warning 会带 OCR 置信度摘要（行数、mean/min 置信度、低置信行数）与「OCR 文本可能有小误读」标记，agent 转述时保留。

## 提取状态（parse_status）与 agent 的业务话

| 状态 | 含义 | agent 必须怎么做 |
|---|---|---|
| `ok` | 提取成功（含 macOS Vision OCR 救回的扫描件/图片，backend=vision_ocr） | 正常进判定；OCR 救回件的「可能有小误读」与人工确认警告照转 |
| `no_text_layer` | 无文字层且 OCR 未能救回：非 macOS 机器，或 OCR 文本不可信（仍是水印/重复串/0 数字字符） | 进 ❌ 清单：如实告知"无法解析，请提供文字版简历"，**不硬造任何字段**（D11） |
| `garbled` | 提取出文本但乱码 | 同上，建议重新导出标准 Word/PDF |
| `encrypted` | WPS/Office 加密 | 告知"文件被加密，请提供未加密版本"，不猜测内容 |
| `unsupported` / `error` | 格式不支持/读取失败 | 如实告知格式与失败原因 |

P3 起失败清单语义收窄为「**仅加密/损坏/OCR 不可信才失败**」（macOS 上）。判定扫描件：字符数阈值 + 水印串重复占比 + 数字字符数（`detect_scanned`）。提不出来就停，**绝不猜测编造**。

## 字段预抽（正则，脚本完成）

脚本从纯文本预抽：姓名（正文优先、文件名兜底并标 `name_source`）、手机号、邮箱、最高学历、院校、院校排名、专业、期望职位/地点/薪资、证书、技能，以及教育/工作/技能/证书四个**原文段**（`sections`，判定回合的 evidence 来源，D4：教育/专业/证书段必须保留全文，只截断工作经历正文）。

**工作年限三档来源（D13，重点）**：

- `text`：正文明确写了年限 → 可直接用于硬门槛；
- `filename`：从文件名抽的（如"胡裕_14年.pdf"）→ 低置信，报告里有 warning；
- `estimated`：正文没写，按工作日期区间/毕业年份**估算** → digest 会标 `needs_review:["years"]`，**agent 必须在批量判定回合用 evidence 原文复核**并在 `candidate_overrides` 回填修正值；估算值直接喂"经验年限一票否决"风险高，复核不了就不要拿它当否决依据。

实测 28 份简历来源分布：正文 16 / 文件名 3 / 估算 9。

## 文件 MD5 与库内附件查重（两套键，别混）

提取时同步算文件**真 MD5**，但它只用于两处：本批内部去重（同一个文件传两次）与 `checkpoint.json` 幂等续跑（重跑跳过已成功项）。

**库内**附件比对用的是另一套键：`(附件文件名, 附件字节大小)` —— 因为钉钉附件读回不带内容哈希，要做内容级比对就得把库内附件全下载回来重算。所以命中时告知的是"库内已存在同名同大小的简历附件（文件名+字节大小 比对命中）"，**不要**对用户说成"MD5 相同/内容完全相同"：

- 改了内容但文件名与字节大小都没变 → 会被误判成重复；
- 同一份内容换个文件名（或大小差一个字节）→ 会漏判，靠手机号查重兜底。

库内内容级（真 MD5）比对属后续阶段的事。

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
