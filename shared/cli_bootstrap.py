# -*- coding: utf-8 -*-
"""CLI 入口脚本的公共样板（重构：消除 5 处重复的 sys.path 注入 + ensure_utf8_io）。

5 个入口脚本（intake_resume / intake_job / build_match_input / verify_decisions /
apply_decisions）各自重复了同样的 3 段代码：

1. ``_ROOT = Path(__file__).resolve().parents[3]`` + 把 ``shared`` 和 ``shared/vendor``
   插入 ``sys.path``（5 处逐字近似，其中 3 处带 ``.is_dir()`` 守卫、2 处不带）。
2. ``from runtime_compat import ensure_utf8_io; ensure_utf8_io()``（5 处逐字相同）。
3. ``--config`` / ``--out-dir`` / ``--batch-id`` 三个 ``add_argument`` 调用。

本模块提供：

  * ``bootstrap()`` —— 一次性完成 sys.path 注入 + ensure_utf8_io，在入口脚本顶部调用。
  * ``common_parser(prog, description, *, add_config=True, add_out_dir=True,
    add_batch_id=True, extra_args=None)`` —— 返回一个预填了公共参数的
    ``argparse.ArgumentParser``，入口脚本在其基础上追加自己的专有参数。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from runtime_compat import ensure_utf8_io

__all__ = ["bootstrap", "common_parser"]


def bootstrap(file_path: str) -> Path:
    """入口脚本顶部调用：注入 sys.path + 强制 UTF-8 IO。

    返回插件根目录（``Path(__file__).resolve().parents[3]``），供调用方做后续路径推导。
    """
    root = Path(file_path).resolve().parents[3]
    for _p in (root / "shared", root / "shared" / "vendor"):
        _s = str(_p)
        if _p.is_dir() and _s not in sys.path:
            sys.path.insert(0, _s)
    ensure_utf8_io()
    return root


def common_parser(
    prog: str,
    description: str,
    *,
    add_config: bool = True,
    add_out_dir: bool = True,
    add_batch_id: bool = True,
) -> argparse.ArgumentParser:
    """返回预填了 --config / --out-dir / --batch-id 的 ArgumentParser。

    入口脚本在此基础上追加自己的专有参数。``--config`` 总是 required；
    ``--out-dir`` 不是 required（有缺省值 None）；``--batch-id`` 用 SUPPRESS。
    """
    ap = argparse.ArgumentParser(prog=prog, description=description)
    if add_config:
        ap.add_argument("--config", required=True,
                        help="config.json 绝对路径（由 replicate 脚步生成）")
    if add_out_dir:
        ap.add_argument("--out-dir", default=None,
                        help="产物目录绝对路径（不给则在系统临时目录下新建）")
    if add_batch_id:
        ap.add_argument("--batch-id", default=None, help=argparse.SUPPRESS)
    return ap
