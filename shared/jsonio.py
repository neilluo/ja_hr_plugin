# -*- coding: utf-8 -*-
"""统一的 JSON 读写原语（重构：消除 6 处独立的 _write_json / dump_json / load_json）。

此前各模块各自定义了几乎相同的 ``_write_json`` / ``dump_json`` / ``load_json``：

  * ``jobintake/textutil.py``  → ``write_json``（indent=2, ensure_ascii=False）
  * ``match/jsonio.py``        → ``dump_json_doc``（indent=1）、``load_json``
  * ``aitable/client.py``      → ``write_json_file``（临时文件、无 indent）
  * ``intake/checkpoint.py``   → ``_write_json``（非原子）+ ``_write_json_atomic``
  * ``intake/pipeline.py``     → ``_write_json``（非原子）
  * ``intake/report.py``       → ``_write_json``（非原子）

本模块提供两个原语：

  * ``write_json(path, payload, *, indent=2)`` —— 非原子写（mkdir + dump）。
  * ``write_json_atomic(path, payload, *, indent=2)`` —— 原子写（tmp + os.replace）。

各调用方改为 import 本模块的同名函数，行为逐字不变（indent / ensure_ascii /
sort_keys 参数全部保留原值）。``match/jsonio.py`` 的 ``dump_json_doc`` 与
``load_json`` / ``strip_md_fence`` 因其 indent=1 的特殊口径和围栏剥离逻辑，继续
留在原模块——只把内部 ``json.dump`` 调用委托给本模块。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = ["write_json", "write_json_atomic"]


def write_json(path: Any, payload: Any, *, indent: int = 2) -> None:
    """非原子写 JSON（ensure_ascii=False, sort_keys=False）。

    调用方负责选择 indent：产物默认 indent=2；digest 侧用 indent=1。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(str(p), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=indent)


def write_json_atomic(path: Any, payload: Any, *, indent: int = 2) -> None:
    """原子写 JSON（tmp + os.replace）。

    进程在任意瞬间被 SIGKILL 也不会留下半截 JSON。用于增量 checkpoint。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=indent)
    os.replace(str(tmp), str(p))
