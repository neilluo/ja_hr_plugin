# -*- coding: utf-8 -*-
"""skills_apply.py — 把子任务产出的三列结果写回简历库（自动扩选项 + 回读校验）

    python3 skills/skills-analyze/scripts/skills_apply.py outputs/skills_done.json          # 扩展选项 + 批量更新
    python3 skills/skills-analyze/scripts/skills_apply.py outputs/skills_done.json --verify # 单独一次读取做回读校验

字段名映射（本机表结构）：skills→技能标签，ai_structured→AI结构化提取，ai_deep→AI深度解析。
回读校验必须单独一次调用：AI表格刚写完立刻读会拿到索引前的旧值。
"""
import sys, os, re, json

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
os.chdir(ROOT)
from notable import Notable, NotableError  # noqa: E402

SKILL_FIELD = "ldMkQqp"


def top_up_options(nt, want):
    sheet = nt.sheet("resume")
    flds = nt.call("GET", "/v1.0/notable/bases/%s/sheets/%s/fields" % (nt.base, sheet)).get("value", [])
    f = next((x for x in flds if x.get("id") == SKILL_FIELD), None)
    if not f:
        raise NotableError("找不到技能标签字段")
    choices = (f.get("property") or {}).get("choices") or []
    have = {c.get("name") for c in choices}
    new = sorted(w for w in want if w and w not in have)
    if new:
        full = [{"id": c["id"], "name": c["name"]} for c in choices if c.get("id")] \
            + [{"name": n} for n in new]
        nt.call("PUT", "/v1.0/notable/bases/%s/sheets/%s/fields/%s" % (nt.base, sheet, SKILL_FIELD),
                {"name": f.get("name", "技能标签"), "type": "multipleSelect",
                 "property": {"choices": full}})
    return new


def apply_(nt, path):
    rows = json.load(open(path, encoding="utf-8"))
    want, upd, bad = set(), [], []
    for r in rows:
        if not r.get("id"):
            bad.append({"reason": "缺id"})
            continue
        sk = [str(x).strip() for x in (r.get("skills") or []) if str(x).strip()]
        st = (r.get("ai_structured") or r.get("ai_extract") or "").strip()
        dp = (r.get("ai_deep") or "").strip()
        if not (sk or st or dp):
            bad.append({"id": r["id"], "reason": "三字段皆空"})
            continue
        want.update(sk)
        upd.append({"id": r["id"], "skills": sk, "ai_extract": st, "ai_deep": dp})
    added = top_up_options(nt, want) if want else []
    # 顺带回填工作年限：upload 脚本常抽不出「12年」这类表述，从精析的"工作经验｜N年"补
    have_years = {r["id"]: r["fields"].get("years_experience")
                  for r in nt.list_records("resume", biz_fields=["years_experience"])}
    for row in upd:
        if not have_years.get(row["id"]):
            m = re.search(r"工作经验｜\s*(\d{1,2})\s*年", row.get("ai_extract") or "")
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
    print(json.dumps({"input": len(rows), "updated": ok, "options_added": len(added),
                      "bad": bad, "failed": failed}, ensure_ascii=False))


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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 1:
        print(__doc__)
        sys.exit(1)
    nt = Notable()
    (verify if "--verify" in sys.argv else apply_)(nt, args[0])


if __name__ == "__main__":
    main()
