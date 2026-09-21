# parsing-methods — 文本提取与字段抽取

## 文本提取（shared/extract.py）

| 格式 | 主路径 | 回退 |
|---|---|---|
| .pdf | `pdftotext -enc UTF-8 <file> -` | vendor/pypdf |
| .docx | zipfile 读 word/document.xml 去标签 | `textutil -convert txt -stdout` |
| .doc | `textutil -convert txt -stdout` | vendor/olefile 读 WordDocument 流 |
| .png/.jpg | 不提取 | 标记 `needs_ocr=True` |

subprocess 一律 capture_output + timeout=30；返回 `{text, needs_ocr, error}`，
异常路径键齐全。

## 扫描件判据（重要）

**抽不出手机号且抽不出邮箱 → 进 needs_ocr 队列，不入库。**
不用文本长度判：扫描件 pdftotext 输出是乱码但非空（实测 134~1051 字）。

## 简历字段（shared/parse_resume.py）

姓名优先序：①「姓名：X」标签 ②文件名拆段（剔除【岗位_城市_薪资】前缀、黑名单段）
③首行启发式；三者都过黑名单（自我评价/专业技能/核心优势/学历词/裸地名）。
学校/专业带左右边界与裸词黑名单；年限正文未命中时用文件名「N年」兜底；
薪资支持区间（15k-20k）。技能/分类为词表命中启发式，允许少量偏差。

## JD 字段（shared/parse_job.py）

- 组织/部门从文件名拆：`岗位说明书-制造中心-曲靖制造基地-<部门段> - <岗位名>.doc`，
  部门归一到 Base 枚举（含两级部门连字符、别名映射如 电池设备部→电池制造部-设备部）。
- 正文按标题切段：岗位职责/工作职责、任职要求/资格条件、硬性门槛。
- must/bonus 技能从任职要求按词表命中；权重默认 0.7/0.3。
- `job_id = J + md5(部门|岗位名)[:10]`，天然幂等。

## 已知边界

- 竖排水印扫描件（邹文飞/訾金保）phone 抽不出 → needs_ocr，属源文件质量问题。
- .doc 的 textutil 偶发空输出（时序），重跑可恢复；空 section 不影响部门/岗位名。
