# -*- coding: utf-8 -*-
"""sync_ai_columns.py — 手工修正薄通道（实现委托 skills_apply）

零散手工修正（不经精析队列）语义三列与档案字段回写：
  1) 智能体读简历原文，逐人产出 payload.json：
     { "手机号": {"skills": ["暖通","空调系统",...],
                  "extract": "**联系方式**\\n...",     # 写入「AI结构化提取」文本列
                  "deep": "**优势分析**\\n...",        # 写入「AI深度解析」文本列
                  "name": "...", "school": "...", ...} }   # 其余键为需要修正的档案字段
  2) 按手机号匹配记录（缺手机号用 name 兜底），转换成 skills_apply 行格式
  3) 写入委托 skills_apply.apply_rows（stamp=False：扩选项/剔词重试/逐条写唯一实现，
     本脚本不复制；手工修正不打 ai_refined_at 出队标记，队列状态不受影响）

用法:
    python3 skills/skills-analyze/scripts/sync_ai_columns.py payload.json
"""
import sys, os, json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "shared"))
sys.path.insert(0, HERE)
os.chdir(os.path.normpath(os.path.join(HERE, "..", "..", "..")))
from notable import Notable  # noqa: E402
from skills_apply import apply_rows  # noqa: E402  写入唯一实现，禁止本地再抄扩选项/剔词重试


def main():
    if "-h" in sys.argv or "--help" in sys.argv:   # --help 早退：不构造 Notable、不触网
        print(__doc__)
        sys.exit(0)
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
    nt = Notable()

    rows = nt.list_records("resume", biz_fields=["name", "phone"])
    by_phone = {str(r["fields"].get("phone")): r["id"] for r in rows if r["fields"].get("phone")}
    by_name = {r["fields"].get("name"): r["id"] for r in rows if r["fields"].get("name")}

    upd, unmatched = [], []
    for key, p in payload.items():
        rid = by_phone.get(str(key)) or by_name.get(p.get("name") or key)
        if not rid:
            unmatched.append(key)
            continue
        row = dict(p)
        row["id"] = rid
        # payload 键是手机号：本人档案缺手机号时顺手补上（非手机号键不写 phone）
        if str(key).isdigit() and not row.get("phone"):
            row["phone"] = str(key)
        upd.append(row)

    rep = apply_rows(nt, upd, stamp=False, require_three=False)
    print(json.dumps({"total": len(payload), "synced": rep["updated"],
                      "options_added": rep["options_added"], "unmatched": unmatched,
                      "bad": rep["bad"], "failed": rep["failed"]}, ensure_ascii=False))
    sys.exit(2 if unmatched else 0)


if __name__ == "__main__":
    main()
