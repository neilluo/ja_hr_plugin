# -*- coding: utf-8 -*-
"""date 列显示格式（钉钉字段 property.formatter）唯一真源与对齐实现。

config.formats.date 声明每个 date 业务键的显示格式（如 "YYYY-MM-DD HH:mm"）；
未声明者建表时用平台默认（实测 YYYY-MM-DD），本模块不猜默认值。
建表（replicate_base）与补列（sync_schema）经 property() 派生；
真表存量列经 align() 对齐（GET 字段列表 → 与 config 声明 diff → PUT property）。
存储值不受影响：date 列写入恒为毫秒时间戳（notable._cast），formatter 只管显示。
"""
import json
import os

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config.json")


def declared(config=None):
    """{表key: {业务键: formatter}}，仅含 config.formats.date 声明者。"""
    cfg = config if config is not None else json.load(open(_CONFIG, encoding="utf-8"))
    return {t: dict(m) for t, m in (cfg.get("formats", {}).get("date") or {}).items()}


def property_for(tkey, biz, config=None):
    """建列 body 的 property 段：声明者返回 {"formatter": ...}，否则 None（用平台默认）。"""
    fmt = declared(config).get(tkey, {}).get(biz)
    return {"formatter": fmt} if fmt else None


def align(nt, config=None):
    """真表 date 列 formatter 对齐 config 声明：只改声明者、只动 property、不改名/类型/选项。
    返回 [{"table", "biz", "cn", "from", "to"}]。"""
    decl = declared(config)
    done = []
    for tkey, fmap in decl.items():
        sheet = nt.cfg["tables"][tkey]["table_id"]
        fields = nt.call("GET", "/v1.0/notable/bases/%s/sheets/%s/fields" % (nt.base, sheet)).get("value", [])
        by_name = {f["name"]: f for f in fields}
        for biz, fmt in fmap.items():
            cn = nt.cn(tkey, biz)
            f = by_name.get(cn)
            if not f or f.get("type") != "date":
                raise RuntimeError("表 %s 的 %s（%s）不存在或类型非 date，无法对齐 formatter" % (tkey, biz, cn))
            cur = (f.get("property") or {}).get("formatter")
            if cur == fmt:
                continue
            nt.call("PUT", "/v1.0/notable/bases/%s/sheets/%s/fields/%s" % (nt.base, sheet, f["id"]),
                    {"name": cn, "type": "date", "property": {"formatter": fmt}})
            done.append({"table": tkey, "biz": biz, "cn": cn, "from": cur, "to": fmt})
    return done
