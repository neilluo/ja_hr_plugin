# -*- coding: utf-8 -*-
"""附件上传：3 步/文件（申请 OSS 直传地址 → urllib PUT → 拿 fileToken）+ 并发池。

⚠️ **附件严禁直传 URL。** cells 里写 `{"url":"https://..."}` 会让服务端同步下载，
10 条记录就 TIMEOUT_ERROR。必须 `attachment upload` 拿 `fileToken` → urllib PUT 到 OSS
→ cells 里写 `[{"fileToken":"ft_xxx"}]`（写入是**整体覆盖不是追加**，格式化纪律见
`schema.TableSchema._format_attachment`）。

附件**没有批量接口**（3 步/文件），所以并发是唯一优化手段：
`upload_attachments(paths, concurrency=5)`（API 限 20 QPS，5 是留足余量的默认值）。
并发上限夹在 1~20 也是原值，别调。
"""

from __future__ import annotations

import concurrent.futures
import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from aitable.client import DwsClient, DwsError

__all__ = ["AttachmentUploader", "DEFAULT_UPLOAD_CONCURRENCY",
           "MAX_ATTACHMENT_SIZE", "OSS_PUT_TIMEOUT"]

#: 附件上传并发默认值（API 限 20 QPS，留余量）
DEFAULT_UPLOAD_CONCURRENCY = 5
#: 附件单文件大小上限（沿用官方 skill 脚本 upload_attachment.py 的取值）
MAX_ATTACHMENT_SIZE = 100 * 1024 * 1024
#: OSS PUT 超时（秒）
OSS_PUT_TIMEOUT = 180


class AttachmentUploader(object):

    def __init__(self, client: DwsClient, base_id: str):
        self.client = client
        self.base_id = base_id

    def upload_attachment(self, file_path: Any,
                          concurrency: int = DEFAULT_UPLOAD_CONCURRENCY) -> Dict[str, Any]:
        """上传单个附件，返回::

            {"ok": bool, "path": str, "file_name": str, "size": int, "mime_type": str,
             "fileToken": str|None, "cell": [{"fileToken": "ft_.."}]|None,
             "elapsed_ms": int, "error": str|None, "code": ..., "category": ...}

        `concurrency` 参数是为与 `upload_attachments` 对齐而保留（单文件用不上）；
        传 list 时自动等价于 `upload_attachments(file_path, concurrency)` 的第一项。
        """
        if isinstance(file_path, (list, tuple)):
            results = self.upload_attachments(list(file_path), concurrency=concurrency)
            return results[0] if results else {"ok": False, "error": "空文件列表"}
        return self._upload_one(file_path)

    def upload_attachments(self, file_paths: Sequence[str],
                           concurrency: int = DEFAULT_UPLOAD_CONCURRENCY
                           ) -> List[Dict[str, Any]]:
        """并发上传多个附件（默认并发 5，API 限 20 QPS 留余量）。

        返回顺序与入参一致。每项含 `cell`，可直接塞进 rows 的 attachment 字段::

            rows[i]["attachment"] = results[i]["cell"]
        """
        paths = list(file_paths or [])
        if not paths:
            return []
        conc = max(1, min(int(concurrency or 1), 20))
        if conc == 1 or len(paths) == 1:
            return [self._upload_one(p) for p in paths]
        results: List[Optional[Dict[str, Any]]] = [None] * len(paths)
        with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as ex:
            fut2idx = {ex.submit(self._upload_one, p): i for i, p in enumerate(paths)}
            for fut in concurrent.futures.as_completed(fut2idx):
                i = fut2idx[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:      # 不让一个文件炸掉整批（失败可见）
                    results[i] = {"ok": False, "path": str(paths[i]),
                                  "file_name": Path(str(paths[i])).name,
                                  "error": "%s: %s" % (type(exc).__name__, exc),
                                  "category": "unknown", "fileToken": None, "cell": None,
                                  "elapsed_ms": 0, "size": 0, "mime_type": None}
        return [r for r in results if r is not None]

    def _upload_one(self, file_path: Any) -> Dict[str, Any]:
        t0 = time.monotonic()
        p = Path(str(file_path)).expanduser()
        base = {"ok": False, "path": str(p), "file_name": p.name, "size": 0,
                "mime_type": None, "fileToken": None, "cell": None,
                "error": None, "code": None, "category": None}
        try:
            if not p.exists() or not p.is_file():
                base.update(error="文件不存在或不是文件", category="invalid",
                            elapsed_ms=int((time.monotonic() - t0) * 1000))
                return base
            size = p.stat().st_size
            mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            base["size"], base["mime_type"] = size, mime
            if size <= 0:
                base.update(error="文件为空", category="invalid",
                            elapsed_ms=int((time.monotonic() - t0) * 1000))
                return base
            if size > MAX_ATTACHMENT_SIZE:
                base.update(error="文件过大 %d 字节（上限 %d）" % (size, MAX_ATTACHMENT_SIZE),
                            category="invalid",
                            elapsed_ms=int((time.monotonic() - t0) * 1000))
                return base

            # 步骤 1：申请 OSS 直传地址（1 次 dws 调用）
            res = self.client.call(["aitable", "attachment", "upload",
                                    "--base-id", self.base_id, "--file-name", p.name,
                                    "--size", str(size), "--mime-type", mime], timeout=120)
            data = res["data"] or {}
            upload_url = data.get("uploadUrl") or data.get("upload_url")
            token = data.get("fileToken") or data.get("file_token")
            if not upload_url or not token:
                base.update(error="attachment upload 未返回 uploadUrl/fileToken：%s"
                                  % json.dumps(data, ensure_ascii=False)[:200],
                            category="invalid",
                            elapsed_ms=int((time.monotonic() - t0) * 1000))
                return base

            # 步骤 2：PUT 到 OSS（Content-Type 必须是文件的具体 MIME type）
            # emit 模式下 uploadUrl 是假的（emit-fake.example.com），跳过 OSS PUT
            if "emit-fake" in (upload_url or ""):
                # emit 模拟：跳过 OSS PUT，直接返回 token
                base.update(ok=True, fileToken=token, cell=[{"fileToken": token}],
                            elapsed_ms=int((time.monotonic() - t0) * 1000))
                return base
            err = self._put_to_oss(upload_url, p, mime, data.get("headers") or {})
            if err:
                base.update(error=err, category="network",
                            elapsed_ms=int((time.monotonic() - t0) * 1000))
                return base

            base.update(ok=True, fileToken=token, cell=[{"fileToken": token}],
                        elapsed_ms=int((time.monotonic() - t0) * 1000))
            return base
        except DwsError as exc:
            base.update(error=exc.message[:300], code=exc.code, category=exc.category,
                        elapsed_ms=int((time.monotonic() - t0) * 1000))
            return base
        except OSError as exc:
            base.update(error="读文件失败：%s" % exc, category="invalid",
                        elapsed_ms=int((time.monotonic() - t0) * 1000))
            return base

    @staticmethod
    def _put_to_oss(upload_url: str, path: Path, mime: str,
                    extra_headers: Optional[Dict[str, str]] = None,
                    attempts: int = 3) -> Optional[str]:
        """把文件 PUT 到 OSS 预签名地址。返回 None 表示成功，否则返回错误描述。"""
        last = "unknown"
        for i in range(attempts):
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
                req = urllib.request.Request(upload_url, data=body, method="PUT")
                req.add_header("Content-Type", mime)
                for k, v in (extra_headers or {}).items():
                    req.add_header(str(k), str(v))
                with urllib.request.urlopen(req, timeout=OSS_PUT_TIMEOUT) as resp:
                    if 200 <= int(resp.status) < 300:
                        return None
                    last = "OSS 返回 HTTP %s" % resp.status
            except urllib.error.HTTPError as exc:
                last = "OSS PUT HTTP %s: %s" % (exc.code, exc.reason)
                if exc.code in (401, 403):        # 预签名过期/签名不符，重试无益
                    return last
            except urllib.error.URLError as exc:
                last = "OSS PUT 网络错误: %s" % getattr(exc, "reason", exc)
            except OSError as exc:
                last = "OSS PUT 失败: %s" % exc
            if i < attempts - 1:
                time.sleep(1.0 * (2 ** i))
        return last
