# -*- coding: utf-8 -*-
"""sync_ai_columns.py — 招聘智能体语义三列回写（技能标签 / AI结构化提取 / AI深度解析）

标准流程（每次简历入库后必须执行）：
  1) 智能体读简历原文（脚本解析摘要 + 必要时视觉补录），逐人语义产出 payload.json：
     { "手机号": {"skills": ["暖通","空调系统",...],
                  "extract": "**联系方式**\\n...",     # 写入「AI结构化提取」文本列
                  "deep": "**优势分析**\\n...",        # 写入「AI深度解析」文本列
                  "name": "...", "school": "...", ...} }   # 其余键为需要修正的档案字段
  2) 自动扩池：payload 里出现但选项池没有的技能标签，先按 choices 结构全量回写
     （原选项必须带 id 回传，否则会丢单元格）
  3) 按手机号匹配记录并批量 update；缺手机号的记录用 name 兜底匹配

用法:
    python3 skills/skills-analyze/scripts/sync_ai_columns.py payload.json
"""
import sys, os, json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared"))
from notable import Notable, NotableError  # noqa: E402

SKILL_FIELD = "ldMkQqp"


def top_up_options(nt, sheet, want):
    """把 payload 里新增的技能标签追加进选项池，原有选项带 id 全量回传。"""
    flds = nt.call("GET", "/v1.0/notable/bases/%s/sheets/%s/fields" % (nt.base, sheet)).get("value", [])
    fld = next((f for f in flds if f.get("id") == SKILL_FIELD), None)
    if not fld:
        raise NotableError("找不到技能标签字段 %s" % SKILL_FIELD)
    choices = ((fld.get("property") or {}).get("choices")) or []
    have = {c.get("name") for c in choices}
    new = sorted(want - have)
    if not new:
        return 0
    full = [{"id": c["id"], "name": c["name"]} for c in choices if c.get("id")] \
        + [{"name": n} for n in new]
    nt.call("PUT", "/v1.0/notable/bases/%s/sheets/%s/fields/%s" % (nt.base, sheet, SKILL_FIELD),
            {"name": fld.get("name", "技能标签"), "type": "multipleSelect",
             "property": {"choices": full}})
    return len(new)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
    nt = Notable()
    sheet = nt.sheet("resume")

    want = set()
    for v in payload.values():
        want.update(v.get("skills", []))
    added = top_up_options(nt, sheet, want)

    rows = nt.list_records("resume", biz_fields=["name", "phone"])
    by_phone = {str(r["fields"].get("phone")): r["id"] for r in rows if r["fields"].get("phone")}
    by_name = {r["fields"].get("name"): r["id"] for r in rows if r["fields"].get("name")}

    upd, unmatched = [], []
    for key, p in payload.items():
        rid = by_phone.get(str(key)) or by_name.get(p.get("name") or key)
        if not rid:
            unmatched.append(key)
            continue
        row = {"id": rid}
        for k, v in p.items():
            if k == "extract":
                row["ai_extract"] = v
            elif k == "deep":
                row["ai_deep"] = v
            elif k == "phone" and str(key).isdigit():
                row["phone"] = str(key)
            else:
                row[k] = v
        row.setdefault("phone", str(key))
        upd.append(row)

    nt.update_records("resume", upd)
    print(json.dumps({"total": len(payload), "synced": len(upd),
                      "options_added": added, "unmatched": unmatched}, ensure_ascii=False))
    sys.exit(2 if unmatched else 0)


if __name__ == "__main__":
    main()
