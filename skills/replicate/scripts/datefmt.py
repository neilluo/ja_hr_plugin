# -*- coding: utf-8 -*-
"""date 列显示格式（钉钉字段 property.formatter）唯一真源与派生实现。

config.formats.date 声明每个 date 业务键的显示格式（如 "YYYY-MM-DD HH:mm"）；
未声明者建表时用平台默认（实测 YYYY-MM-DD），本模块不猜默认值。
建表（replicate_base）与补列（sync_schema）经 property() 派生。
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
