#!/usr/bin/env python3
"""sync_schema.py — 表结构补列/自检工具（config.json 为 SSOT，只补不删）。

用法:
    python3 skills/replicate/scripts/sync_schema.py --check   # 只读漂移报告，有缺失列 exit 2
    python3 skills/replicate/scripts/sync_schema.py           # 对缺失列执行补列（POST fields）
    python3 skills/replicate/scripts/sync_schema.py --rename "表名:旧列名=新列名"  # 物理列改名（可重复）
    python3 skills/replicate/scripts/sync_schema.py --drop "表名:列名"            # 删物理列（可重复）

逻辑：对四张表各 GET /v1.0/notable/bases/{base}/sheets/{sheet}/fields 拿物理列名集合，
与 cfg["fields"][t] 的中文名集合 diff：
  missing = config 有而物理没有（补列对象）；
  extra   = 物理有而 config 没有（只报告不处理，绝不删列、绝不改已有列）。
补列按 config 字段书写顺序逐列 POST fields（body 同 replicate_base：name/type/options），
每列 sleep 0.2 限流余量。逐表打印 JSON 报告 {"table", "missing", "extra", "created"}，
最后汇总；--check 下有 missing 则 exit 2。

--rename / --drop 是显式对齐操作（config 改名/删字段后把物理表跟上），不做任何自动猜测：
  --rename 按旧名定位字段 ID 后 PUT 改名（保留原 type 与 select 选项）；新名已存在则报错。
  --drop 仅允许删 config 未声明的列（extra）；删 config 已声明的列直接拒绝。
执行顺序：rename → drop → 补列。
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

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config.json")


def _parse_spec(spec):
    """'表名:列名=新列名' / '表名:列名' → (表名, 列名, 新列名或None)"""
    if ":" not in spec:
        raise NotableError("参数格式应为 表名:列名[=新列名]，收到 %r" % spec)
    tname, col = spec.split(":", 1)
    new = None
    if "=" in col:
        col, new = col.split("=", 1)
    return tname.strip(), col.strip(), (new.strip() if new else None)


def _fields(nt, tname, cfg):
    tkey = next((k for k, v in cfg["tables"].items() if v["name"] == tname), None)
    if not tkey:
        raise NotableError("未知表名 %r（可用：%s）" % (tname, [v["name"] for v in cfg["tables"].values()]))
    sheet = cfg["tables"][tkey]["table_id"]
    r = nt.call("GET", "/v1.0/notable/bases/%s/sheets/%s/fields" % (nt.base, sheet))
    return tkey, sheet, r.get("value", [])


def do_rename(nt, cfg, specs):
    done = []
    for spec in specs:
        tname, old, new = _parse_spec(spec)
        if not new:
            raise NotableError("--rename 需要 表名:旧列名=新列名，收到 %r" % spec)
        tkey, sheet, flds = _fields(nt, tname, cfg)
        f = next((x for x in flds if x.get("name") == old), None)
        if not f:
            raise NotableError("表 %s 无列 %r" % (tname, old))
        if any(x.get("name") == new for x in flds):
            raise NotableError("表 %s 已存在列 %r，拒绝改名" % (tname, new))
        body = {"name": new, "type": f.get("type")}
        prop = f.get("property")
        if prop and f.get("type") in ("singleSelect", "multipleSelect"):
            body["property"] = prop
        nt.call("PUT", "/v1.0/notable/bases/%s/sheets/%s/fields/%s" % (nt.base, sheet, f["id"]), body)
        done.append("%s.%s→%s" % (tname, old, new))
        time.sleep(0.2)
    return done


def do_drop(nt, cfg, specs):
    done = []
    for spec in specs:
        tname, col, new = _parse_spec(spec)
        if new:
            raise NotableError("--drop 不接受 =新列名，收到 %r" % spec)
        tkey, sheet, flds = _fields(nt, tname, cfg)
        declared = set(cfg["fields"][tkey].values())
        if col in declared:
            raise NotableError("表 %s 的 %r 是 config 已声明列，拒绝删除" % (tname, col))
        f = next((x for x in flds if x.get("name") == col), None)
        if not f:
            raise NotableError("表 %s 无列 %r" % (tname, col))
        nt.call("DELETE", "/v1.0/notable/bases/%s/sheets/%s/fields/%s" % (nt.base, sheet, f["id"]))
        done.append("%s.%s" % (tname, col))
        time.sleep(0.2)
    return done


def main():
    ap = argparse.ArgumentParser(description="按 config.json（SSOT）补齐缺失列 / 自检漂移 / 显式改名删列")
    ap.add_argument("--check", action="store_true",
                    help="只读：打印漂移报告，有缺失列 exit 2；不补列")
    ap.add_argument("--rename", action="append", default=[], metavar="表名:旧列名=新列名",
                    help="物理列改名（保留类型与选项）；可重复")
    ap.add_argument("--drop", action="append", default=[], metavar="表名:列名",
                    help="删除 config 未声明的物理列；可重复")
    args = ap.parse_args()

    # stage 0: 环境预检
    run_preflight(config_path=_CONFIG)

    nt = Notable()
    cfg = nt.cfg
    renamed = do_rename(nt, cfg, args.rename) if args.rename else []
    dropped = do_drop(nt, cfg, args.drop) if args.drop else []
    if renamed or dropped:
        print(json.dumps({"renamed": renamed, "dropped": dropped}, ensure_ascii=False))
    options = cfg.get("options", {})
    total_missing = total_created = 0
    for tkey, fmap in cfg["fields"].items():
        tname = cfg["tables"][tkey]["name"]
        sheet = cfg["tables"][tkey]["table_id"]
        phys = nt.call("GET", "/v1.0/notable/bases/%s/sheets/%s/fields" % (nt.base, sheet))
        phys_list = phys.get("value", [])
        phys_names = {f["name"] for f in phys_list}
        # 平台主键列（字段列表首列，如「标题」）非业务列，不计入 extra 噪声
        primary = phys_list[0]["name"] if phys_list else None
        want_names = set(fmap.values())
        missing = [biz for biz, cn in fmap.items() if cn not in phys_names]
        extra = sorted(phys_names - want_names - ({primary} if primary else set()))
        created = []
        if not args.check:
            for biz in missing:
                cn = fmap[biz]
                ftype = cfg["types"][tkey][biz]
                opts = options.get(tkey, {}).get(biz)
                body = {"name": cn, "type": ftype}
                if ftype in ("singleSelect", "multipleSelect") and opts:
                    body["property"] = {"options": [{"name": o} for o in opts]}
                nt.call("POST", "/v1.0/notable/bases/%s/sheets/%s/fields" % (nt.base, sheet), body)
                created.append(cn)
                time.sleep(0.2)  # 建字段限流余量
        total_missing += len(missing)
        total_created += len(created)
        print(json.dumps({"table": tname,
                          "missing": [fmap[b] for b in missing],
                          "extra": extra,
                          "created": created}, ensure_ascii=False))
    print(json.dumps({"mode": "check" if args.check else "sync",
                      "tables": len(cfg["fields"]),
                      "missing_total": total_missing,
                      "created_total": total_created}, ensure_ascii=False))
    if args.check and total_missing:
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except NotableError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
