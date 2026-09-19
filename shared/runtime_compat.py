# -*- coding: utf-8 -*-
"""运行时环境兼容层。

只装两件在**非 macOS / 非 UTF-8 控制台**上会直接崩的事，二者都只依赖标准库：

1. `ensure_utf8_io()` —— Windows 控制台的默认编码是 cp936（GBK）。脚本要打印大量
   中文（报告、告警、VISION_NEEDED 清单），cp936 下会抛 UnicodeEncodeError，且宿主
   进程按 UTF-8 解码我们的输出时还会二次乱码。PEP 540 的 UTF-8 模式（`-X utf8`）能
   解决，但那要求调用方记得加参数；这里在进程内直接改，调用方无需配合。

2. `default_out_root()` —— 产物目录的缺省根。原先硬编码 `/tmp/recruit-fast`，在
   Windows 上 `/tmp` 是**驱动器相对路径**，会落到 `C:\\tmp`；标准用户对 C:\\ 根目录
   没有写权限，`mkdir` 直接失败，整批入库中止。改用 `tempfile.gettempdir()`，它在
   三个平台上都返回当前用户可写的目录（macOS `$TMPDIR`、Windows `%TEMP%`、Linux
   `/tmp`），顺带避免了 `/tmp` 是多用户共享目录的问题。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

__all__ = ["ensure_utf8_io", "default_out_root"]

#: 产物目录缺省根下的固定子目录名（沿用原 `/tmp/recruit-fast` 的名字，便于认）
OUT_ROOT_NAME = "recruit-fast"


def ensure_utf8_io() -> None:
    """把 stdout/stderr 强制成 UTF-8。入口脚本在解析参数前调用。

    `errors` 刻意不动（保持默认 strict）：我们自己的输出全是可编码字符，改成
    replace 只会掩盖真问题，而且会改变已有输出的字节。宿主把 sys.stdout 换成
    非 TextIOWrapper 对象（测试捕获、某些嵌入式运行器）时没有 reconfigure，跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def default_out_root() -> Path:
    """产物目录的缺省根：当前用户可写的系统临时目录下的 `recruit-fast/`。"""
    return Path(tempfile.gettempdir()) / OUT_ROOT_NAME
