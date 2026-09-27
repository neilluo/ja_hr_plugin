你在协助一个光伏企业（晶澳）HR 系统做简历 AI 精析。这是「读文件 + 推理 + 写一个 JSON 文件」的任务：不要调用任何外部 API、不要联网、不要改动其他文件。

## 输入

- 批次文件（下面第一条指令给出的路径 `<BATCH_PATH>`）：数组，每条含
  `id`、`name`、`current_skills`（脚本正则粗提取的，可能不准/不全）、`full_text`（简历全文，可能被截断）
- 词表文件（`<VOCAB_PATH>`，即仓库根下 `outputs/job_vocab.json`）
  —— 岗位必备/加分技能同源词表，**选词优先与它一致**（否则后续匹配打分会对不上）

通读 `full_text`，为每条记录产出三个字段。

## 1. skills（技能标签数组）

提炼该候选人真实、可验证的硬技能/专业标签：
- 简体中文，具体不空泛。禁止"学习能力""团队合作""抗压能力"这类软素质词。
- 字数规则（可机检口径）：**2-6 字只数中文字符；纯英文/缩写术语（如 PLC、CAD、SolidWorks、K8s、
  YOLO、MES、Docker、MySQL）不受字数限制**，但保持单个术语简短。**禁止把英文术语按字符数判超长**
  （如把 "Kubernetes" 10 个字符判失败）。
- 优先从 `<VOCAB_PATH>`（job_vocab.json 岗位必备/加分技能同源词表）与简历全文里选词；
  示例（不是穷举，最终以 job_vocab.json 与简历实际内容为准）：单晶、拉晶、丝网印刷、PLC、SolidWorks。
- 词表外确有的硬技能可直接新增（如"焊接机器人""AOI""SPC"）。
- 数量 5-12 个，按重要性排序；纠正 `current_skills` 里明显错抓的词（如把"Excel""成本"当技能、
  把"助理工程师/高级电工证"这类职级或证书混进技能）。
- 全文是乱码/空/与技能无关的，输出 `[]`。
- **不要编造简历中未提及的技能。**

## 2. ai_structured（AI 结构化提取，字符串）

固定 5 段，每段一行，段名后用"｜"分隔：

```
学历背景｜<最高学历> <院校> <专业> <院校层次判断>
工作经验｜<总年限>年，<最近一份工作的公司+岗位+时长>，<行业关键词>
（<总年限> 只写纯数字如 `4年`，禁止「约4年/近4年/4年以上」；简历未明写时按经历时间段做减法估出数字。
下游有正则从本段回填年限字段，修饰词会让回填静默失败）
核心技能｜<Top 5 硬技能，逗号分隔>
求职意向｜<期望职位> <期望地点> <期望薪资>
匹配度评估｜<一句话判断适合什么岗位/产线，如"适合单晶拉棒产线设备工程师">
```

某段信息缺失用"未提及"填充，不要编造。整段控制在 200 字以内。

## 3. ai_deep（AI 深度解析，字符串）

给 HR 的决策参考：
- **亮点**：1-2 条最突出优势，必须具体到项目/指标/金额/年限（如"年运维成本节约1400万元"）
- **风险**：1-2 条顾虑（跳槽频繁/空窗期/技能与岗位偏差/学历短板等），没有就写"无明显风险"
- **建议**：一句话（"建议安排技术面""可作储备""暂不推荐"等）

整段控制在 200 字以内，自然语言，不用列表符号。

## 输出

把结果写入与批次文件同目录、**文件名由输入自派生**的 done 文件（输入 `skills_pending_part<N>.json` →
输出 `skills_done_part<N>.json`，把 pending 换成 done，N 不变），格式：

```json
[
  {
    "id": "recordId",
    "skills": ["技能1", "技能2"],
    "ai_structured": "学历背景｜本科 昆明理工 材料工程 普通本科\n工作经验｜...",
    "ai_deep": "亮点：… 风险：… 建议：…"
  }
]
```

要求：
1. **必须覆盖输入文件里的全部 id**，一条都不能漏；
2. 写完后**必须原样运行下面这段规范校验脚本**（禁止自写 assert 口径、禁止改动判定边界；
   把脚本里的 `BATCH`/`DONE` 两处占位替换为你自己的批次文件与输出文件路径）：

```bash
python3 -X utf8 - <<'EOF'
import json, re, sys
inp = json.load(open(sys.argv[1] if len(sys.argv) > 1 else 'BATCH'))  # 占位：agent 运行时替换为 <BATCH_PATH>
out = json.load(open(sys.argv[2] if len(sys.argv) > 2 else 'DONE'))   # 占位：替换为 skills_done_part<N>.json
assert {r['id'] for r in inp} == {r['id'] for r in out}, 'id 集合不一致'
for r in out:
    for f in ('skills', 'ai_structured', 'ai_deep'):
        assert f in r, f'缺字段 {f}'
    segs = [l.split('｜')[0] for l in r['ai_structured'].split('\n')]
    assert segs == ['学历背景', '工作经验', '核心技能', '求职意向', '匹配度评估'], f'段名错 {segs}'
    assert len(r['ai_structured']) <= 200 and len(r['ai_deep']) <= 200, '超 200 字'
    assert len(r['skills']) == 0 or 5 <= len(r['skills']) <= 12, '技能数越界'
    for s in r['skills']:
        zh = len(re.findall(r'[\u4e00-\u9fff]', s))
        assert 0 < len(s) <= 12 and (zh == 0 or 2 <= zh <= 6), f'标签字数违规 {s}'
print('OK', len(out))
EOF
```

3. 流程纪律：**先按规范一次写对再校验；校验失败只允许一次性修正后复验一次**，禁止多轮试探性 Edit；
4. Windows 下命令用 `py`（`python3`/`python` 会静默失败）；
5. 回复只需报告条数和 2-3 个典型标签例子，200 字以内。
