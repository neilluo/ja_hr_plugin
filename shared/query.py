#!/usr/bin/env python3
"""只读查询：列出/过滤四张表记录，或输出岗位维度匹配统计。

用法:
    python3 shared/query.py <resume|job|match|perm> [--filter 业务键=值 ...] [--fields 业务键,业务键]
    python3 shared/query.py match --stats        # 按岗位聚合推荐/待定/不推荐数
示例:
    python3 shared/query.py resume --filter phone=13900000001
    python3 shared/query.py job --fields job_id,job_name,department
"""

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notable import Notable  # noqa: E402
from preflight import run_preflight  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def main():
    ap = argparse.ArgumentParser(description="AI 表格只读查询")
    ap.add_argument("table", choices=["resume", "job", "match", "perm"])
    ap.add_argument("--filter", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--fields", default="", help="逗号分隔的业务键，缺省全字段")
    ap.add_argument("--stats", action="store_true", help="match 表按岗位聚合统计")
    args = ap.parse_args()

    # stage 0: 环境预检
    run_preflight(config_path=_CONFIG)

    nt = Notable()
    bad = [kv for kv in args.filter if "=" not in kv]
    if bad:
        ap.error("--filter 需要 键=值 形式: %s" % bad)
    if args.stats and args.table != "match":
        ap.error("--stats 仅支持 match 表")
    flt = dict(kv.split("=", 1) for kv in args.filter)
    fields = [f for f in args.fields.split(",") if f] or None
    rows = nt.list_records(args.table, flt=flt or None, biz_fields=fields)

    if args.stats:
        agg = {}
        for r in rows:
            f = r["fields"]
            a = agg.setdefault(f.get("job_id") or "?",
                               {"job_name": f.get("job_name"), "total": 0, "counts": Counter()})
            a["total"] += 1
            a["counts"][f.get("recommend") or "未判定"] += 1
        for a in agg.values():
            a["counts"] = dict(a["counts"])
        print(json.dumps(agg, ensure_ascii=False, indent=2))
        return

    out = [{"id": r["id"],
            "fields": {k: v for k, v in r["fields"].items() if not fields or k in fields}}
           for r in rows]
    print(json.dumps({"count": len(out), "records": out}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
