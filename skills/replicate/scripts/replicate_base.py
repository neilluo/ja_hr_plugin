#!/usr/bin/env python3
"""在新 Base 复制四表结构（表+字段+单选选项），并打印新 config.json 片段。

用法:
    python3 skills/replicate/scripts/replicate_base.py <新baseId> [--operator <unionId>]

说明：钉钉 AI 表格无组织级公共表，跨组织分发=新建 Base 后跑本脚本重建结构，
再用输出的 config 片段替换 config.json 的 base_id/tables 段。
表结构唯一事实源（SSOT）为仓库根 config.json：表名取 tables[key].name，
字段按 fields[key] 书写顺序建列，类型取 types[key]，select 选项取 options[key]。
权限：应用需 Notable.Base.Write.All；operator 需对该 Base 有编辑权限。
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared", "preflight"))
from notable import Notable, NotableError  # noqa: E402
from preflight import run_preflight  # noqa: E402
import datefmt  # noqa: E402  date 列显示格式唯一真源 config.formats.date，禁止本地抄第二份

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config.json")


def main():
    ap = argparse.ArgumentParser(description="在新 Base 重建四表结构")
    ap.add_argument("base_id")
    ap.add_argument("--operator", default=None, help="覆盖 config 的 operator_id")
    args = ap.parse_args()

    # stage 0: 环境预检（Notable() 要读现有 config.json 拿凭证与 operator_id 缺省值）
    run_preflight(config_path=_CONFIG)

    nt = Notable()
    cfg = nt.cfg
    nt.base = args.base_id
    if args.operator:
        nt.op = args.operator
    out = {"base_id": args.base_id, "tables": {}, "fields": {}, "types": {}}
    options = cfg.get("options", {})
    for key, fmap in cfg["fields"].items():
        tname = cfg["tables"][key]["name"]
        r = nt.call("POST", "/v1.0/notable/bases/%s/sheets" % nt.base,
                    {"name": tname, "fields": []})
        sheet = r["id"]
        out["tables"][key] = {"table_id": sheet, "name": tname}
        out["fields"][key], out["types"][key] = {}, {}
        for biz, cn in fmap.items():
            ftype = cfg["types"][key][biz]
            opts = options.get(key, {}).get(biz)
            body = {"name": cn, "type": ftype}
            if ftype in ("singleSelect", "multipleSelect") and opts:
                body["property"] = {"options": [{"name": o} for o in opts]}
            elif ftype == "date":
                prop = datefmt.property_for(key, biz, cfg)
                if prop:
                    body["property"] = prop
            nt.call("POST", "/v1.0/notable/bases/%s/sheets/%s/fields" % (nt.base, sheet), body)
            out["fields"][key][biz] = cn
            out["types"][key][biz] = ftype
            time.sleep(0.2)  # 建字段限流余量
        print("built", tname, sheet, len(fmap), "fields")
    out["note"] = "replicate_base.py 生成；合并进 config.json 后替换 base_id/tables/fields/types"
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except NotableError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
