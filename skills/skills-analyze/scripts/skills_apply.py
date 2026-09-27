# -*- coding: utf-8 -*-
"""skills_apply.py — 把子任务产出的三列结果写回简历库（自动扩选项 + 回读校验）

    python3 skills/skills-analyze/scripts/skills_apply.py outputs/skills_done.json          # 扩展选项 + 批量更新
    python3 skills/skills-analyze/scripts/skills_apply.py outputs/skills_done.json --verify # 单独一次读取做回读校验

字段名映射（本机表结构）：skills→技能标签，ai_structured→AI结构化提取，ai_deep→AI深度解析。
每条记录在三列的同一次 update 里打 ai_refined_at 标记（毫秒时间戳）——它是精析队列出队的
唯一凭证（谓词真源 shared/refine_loop.py，此处不复述条件）。
写回（非 --verify）完成后释放 resume 链周期租约（prepare 获取，见 shared/refine_loop.py）。
回读校验必须单独一次调用：AI表格刚写完立刻读会拿到索引前的旧值。
"""
import sys, os, re, json, time

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
os.chdir(ROOT)
from notable import Notable, NotableError  # noqa: E402
import refine_loop  # noqa: E402  周期锁释放方（prepare 获取、apply 释放，见 shared/refine_loop.py）


def top_up_options(nt, want):
    sheet = nt.sheet("resume")
    flds = nt.call("GET", "/v1.0/notable/bases/%s/sheets/%s/fields" % (nt.base, sheet)).get("value", [])
    skill_cn = nt.cn("resume", "skills")
    f = next((x for x in flds if x.get("name") == skill_cn), None)
    if not f:
        raise NotableError("找不到技能标签字段")
    choices = (f.get("property") or {}).get("choices") or []
    have = {c.get("name") for c in choices}
    new = sorted(w for w in want if w and w not in have)
    if new:
        full = [{"id": c["id"], "name": c["name"]} for c in choices if c.get("id")] \
            + [{"name": n} for n in new]
        nt.call("PUT", "/v1.0/notable/bases/%s/sheets/%s/fields/%s" % (nt.base, sheet, f["id"]),
                {"name": f.get("name", "技能标签"), "type": "multipleSelect",
                 "property": {"choices": full}})
    return new


def apply_rows(nt, rows, stamp=True, require_three=True):
    """三列写回的可复用写函数（唯一实现，sync_ai_columns 亦委托此函数）。

    rows: [{"id":..., "skills":[...], "ai_extract"(或"ai_structured")/extract:..., "ai_deep"(或deep):...}]
    stamp=True（精析流水线）：固定写三列并打 ai_refined_at 出队标记（与三列同一次 update）；
    require_three=True：三列皆空的行进 bad 不写。
    stamp=False（sync_ai_columns 手工修正通道）：extract/deep 归一成列名，行内其余档案字段原样写回。
    返回报告 dict {"input","updated","options_added","bad","failed"}。
    """
    want, upd, bad = set(), [], []
    stamped = int(time.time() * 1000)   # 出队标记：与三列同一次 update 写入（每条都打）
    for r in rows:
        if not r.get("id"):
            bad.append({"reason": "缺id"})
            continue
        sk = [str(x).strip() for x in (r.get("skills") or []) if str(x).strip()]
        st = (r.get("ai_structured") or r.get("ai_extract") or r.get("extract") or "").strip()
        dp = (r.get("ai_deep") or r.get("deep") or "").strip()
        if require_three and not (sk or st or dp):
            bad.append({"id": r["id"], "reason": "三字段皆空"})
            continue
        if stamp:
            row = {"id": r["id"], "skills": sk, "ai_extract": st, "ai_deep": dp,
                   "ai_refined_at": stamped}
        else:   # 手工修正通道：其余档案字段原样写回，不打出队标记
            row = {"id": r["id"]}
            for k, v in r.items():
                if k in ("id", "skills", "ai_structured", "extract", "deep"):
                    continue
                row[k] = v
            if "skills" in r:
                row["skills"] = sk
            if "ai_extract" in r or "extract" in r:
                row["ai_extract"] = st
            if "ai_deep" in r or "deep" in r:
                row["ai_deep"] = dp
        want.update(sk)
        upd.append(row)
    added = top_up_options(nt, want) if want else []
    # 顺带回填工作年限：upload 脚本常抽不出「12年」这类表述，从精析的"工作经验｜N年"补
    # 容忍 subagent 偶写的约/近修饰（prompt 已禁，正则兜底防静默漏回填）
    have_years = {r["id"]: r["fields"].get("years_experience")
                  for r in nt.list_records("resume", biz_fields=["years_experience"])}
    for row in upd:
        if not have_years.get(row["id"]):
            m = re.search(r"工作经验｜\s*(?:约|近)?\s*(\d{1,2})\s*年", row.get("ai_extract") or "")
            if m:
                row["years_experience"] = int(m.group(1))
    ok, failed = 0, []
    for row in upd:                       # 逐条写：非法选项只影响本条，剔词重试
        for _ in range(12):
            try:
                nt.update_records("resume", [row])
                ok += 1
                break
            except NotableError as e:
                m = re.search(r"the option '\"([^\"]+)\"' is invalid", str(e))
                if m and m.group(1) in (row.get("skills") or []):
                    row["skills"] = [s for s in row["skills"] if s != m.group(1)]
                    continue
                failed.append({"id": row["id"], "error": str(e)[:160]})
                break
        else:
            failed.append({"id": row["id"], "error": "剔词重试超限"})
    return {"input": len(rows), "updated": ok, "options_added": len(added),
            "bad": bad, "failed": failed}


def apply_(nt, path):
    rows = json.load(open(path, encoding="utf-8"))
    print(json.dumps(apply_rows(nt, rows), ensure_ascii=False))


def verify(nt, path):
    rows = json.load(open(path, encoding="utf-8"))
    want = {r["id"]: r for r in rows if r.get("id")}
    back = {r["id"]: r["fields"] for r in nt.list_records("resume", biz_fields=["skills", "ai_extract"])}
    miss = [i for i in want if i not in back]
    mismatch = [i for i, r in want.items() if i in back and not (back[i].get("skills") or [])
                and (r.get("skills") or [])]
    print(json.dumps({"checked": len(want), "record_not_found": miss,
                      "readback_mismatch": mismatch}, ensure_ascii=False))


def main():
    if "-h" in sys.argv or "--help" in sys.argv:   # --help 早退：不构造 Notable、不触网
        print(__doc__)
        sys.exit(0)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 1:
        print(__doc__)
        sys.exit(1)
    nt = Notable()
    if "--verify" in sys.argv:
        verify(nt, args[0])
        return
    try:
        apply_(nt, args[0])
    finally:
        # 写回即周期终点：释放 prepare 获取的租约（verify 只读不释，防半途误释放）
        refine_loop.release_lock(refine_loop.lock_path(os.path.join(ROOT, "outputs"), "resume"))


if __name__ == "__main__":
    main()
