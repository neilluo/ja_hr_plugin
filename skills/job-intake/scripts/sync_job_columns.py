# -*- coding: utf-8 -*-
"""sync_job_columns.py — 招聘智能体JD语义回写（硬性门槛 / 必备技能 / 加分项）

入库脚本从正文里抠出的必备技能常是噪声词（如「会计、财务、账务」），会让匹配打分失真。
标准流程：每次 JD 入库后，智能体逐岗分析任职要求与岗位职责，产出 payload.json 回写这三列。

payload.json 格式（键可用 岗位ID 或 "部门|岗位名"）：
  {"J3D5C031FB9": {"hard_gates": "学历：本科及以上；专业：财会相关专业；经验：2年以上；证书：初级会计师及以上；年龄：25-40岁",
                   "must_skills": "成本核算、存货盘点、成本分析、金蝶、账务处理",
                   "bonus_skills": "合并报表、税务申报、ERP、Excel"}}

写法要求：
  - hard_gates 固定五段（缺项写"不作硬性要求"）：学历：…；专业：…；经验：…；证书：…；年龄：…
    这四/五项是一票否决项，也是匹配复核的逐项对照依据。
  - must_skills / bonus_skills 用「、」分隔的短词，词表必须与简历库技能标签同源
    （否则命中率恒为 0，分数全部失真）；每岗必备技能 6~10 个，加分项 4~8 个。
  - 不要塞 Excel/Word/办公软件 这类无区分度词，除非 JD 把它写成核心要求。

用法:  python3 skills/job-intake/scripts/sync_job_columns.py payload.json
"""
import sys, os, json, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared"))
from notable import Notable  # noqa: E402

# 回写字段白名单(安全用途)，非字段清单；字段全集见 config.json fields.job
KEYS = ("hard_gates", "must_skills", "bonus_skills", "work_location", "org", "status")


def main():
    if "-h" in sys.argv or "--help" in sys.argv:   # --help 早退：不构造 Notable、不触网
        print(__doc__)
        sys.exit(0)
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
    nt = Notable()
    rows = nt.list_records("job", biz_fields=["job_id", "job_name", "department"])
    by_id = {r["fields"].get("job_id"): r["id"] for r in rows}
    by_key = {"%s|%s" % (r["fields"].get("department"), r["fields"].get("job_name")): r["id"] for r in rows}

    upd, miss = [], []
    for k, p in payload.items():
        rid = by_id.get(k) or by_key.get(k)
        if not rid:
            miss.append(k)
            continue
        row = {"id": rid}
        for biz in KEYS:
            if p.get(biz):
                row[biz] = p[biz]
        # 精析完成打标记（队列谓词真源 shared/refine_loop.py：ai_refined_at 非空即出队）：
        # 仅当本次真写了三列之一才算精析完成，与三列同一次 update 落库
        if any(k in row for k in ("hard_gates", "must_skills", "bonus_skills")):
            row["ai_refined_at"] = int(time.time() * 1000)
        upd.append(row)

    if upd:
        nt.update_records("job", upd)
    report = {"total": len(payload), "synced": len(upd), "unmatched": miss}
    print(json.dumps(report, ensure_ascii=False))
    sys.exit(2 if miss else 0)


if __name__ == "__main__":
    main()
