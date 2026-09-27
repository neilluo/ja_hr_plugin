# -*- coding: utf-8 -*-
"""check_skill_coverage.py — 岗位技能词表 vs 简历技能标签 的可命中率自检

用途：必备/加分技能若与简历库标签不同源，匹配分数会恒低或全0。每次 JD 入库、每次简历入库
（或标签池调整）后跑一次，把「可命中」比例低于 50% 的岗位列出来修词。

前置门禁：岗位精析队列非空（真源 shared/refine_loop.py）→ exit 2：三列还是入库正则粗值，
覆盖率结论无意义，先跑 JD 精析流水线（jobs_analyze → sync_job_columns）再来自检。
简历标签池为空（resume 表无 skills，如简历尚未精析）→ 输出 skipped 并 exit 0：
命中率无从谈起，跳过自检而非把全岗误判低覆盖。
简历标签池为空（resume 表无 skills，如简历尚未精析）→ 输出 skipped 并 exit 0：
命中率无从谈起，跳过自检而非把全岗误判低覆盖。

用法:  python3 skills/job-intake/scripts/check_skill_coverage.py [--min 0.5]
输出:  每岗一行命中比例 + 汇总；存在低覆盖岗位时 exit 2。
"""
import sys, os, json, argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared"))
from notable import Notable  # noqa: E402
from vocab import toks  # noqa: E402  统一分词（唯一真源 shared/vocab.py，禁止本地再抄 SEP）
import refine_loop  # noqa: E402  队列谓词唯一真源，禁止本地抄副本


def hit(cand, need):
    if not cand or not need:
        return False
    if need in cand or cand in need:
        return True
    a, b = set(toks(need)), set(toks(cand))
    return bool(a & b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=0.5, help="必备技能最低可命中比例，默认0.5")
    args = ap.parse_args()

    nt = Notable()
    # 前置门禁（与 match_gated 同纪律）：岗位精析未完成时，三列是入库正则粗值，覆盖率无意义
    if refine_loop.queue(nt, "job"):
        print(json.dumps({"refused": True,
                          "reason": "岗位未精析（队列非空），粗词表覆盖率无意义；先跑 jobs_analyze → sync_job_columns"},
                         ensure_ascii=False))
        sys.exit(2)
    jobs = nt.list_records("job", biz_fields=["job_id", "job_name", "department", "must_skills", "bonus_skills"])
    pool = set()
    for c in nt.list_records("resume", biz_fields=["skills"]):
        pool.update(c["fields"].get("skills") or [])
    cands = sorted(pool)
    if not cands:
        # 简历标签池为空（如简历尚未精析）：命中率无从谈起，跳过自检而非全岗误判低覆盖
        print(json.dumps({"skipped": True, "reason": "简历标签池为空，覆盖率自检无意义",
                          "jobs": len(jobs)}, ensure_ascii=False))
        sys.exit(0)

    low, empty_tag = [], 0
    for j in jobs:
        f = j["fields"]
        must = toks(f.get("must_skills"))
        if not must:
            empty_tag += 1
            continue
        m = [n for n in must if any(hit(c, n) for c in cands)]
        ratio = len(m) / len(must)
        if ratio < args.min:
            low.append((f.get("job_name"), f.get("department"), f.get("job_id"), len(m), len(must), ratio))
            print("低覆盖 可命中 %d/%d  %-22s %s  未命中词参考: %s"
                  % (len(m), len(must), f.get("job_name"), f.get("job_id"),
                     "、".join([n for n in must if n not in m][:6])))
    print("岗位数=%d 必备技能空缺=%d 简历标签池=%d 低覆盖=%d" % (len(jobs), empty_tag, len(cands), len(low)))
    sys.exit(2 if low else 0)


if __name__ == "__main__":
    main()
