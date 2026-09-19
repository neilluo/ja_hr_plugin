# -*- coding: utf-8 -*-
"""JobTableGateway：intake_job.py 的 dws IO 边界唯一入口。

全部方法是薄转发：参数组装保持原调用形状（filter 分片 100/片、chunk_size=10、
settle_tries=2/1、max_pages=100、all_pages=True），dws 调用的顺序与次数由编排层
的既有门控决定，本类不搬任何判定逻辑、不改任何默认值。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["JobTableGateway"]


class JobTableGateway:
    """dws IO 边界。B 侧 tbl 恒非 None（config 失败在 run() 里直接写报告退出）。"""

    def __init__(self, tbl: Any, counter: Any) -> None:
        self.tbl = tbl
        self.counter = counter

    # -- 只读属性转发（不发 dws） -------------------------------------------
    @property
    def config(self) -> Dict[str, Any]:
        return self.tbl.config

    @property
    def config_path(self) -> str:
        return self.tbl.config_path

    @property
    def warnings(self) -> List[str]:
        return self.tbl.warnings

    def field_keys(self, table_key: str) -> List[str]:
        return self.tbl.field_keys(table_key)

    def known_location_options(self) -> List[str]:
        """work_location 现有选项池（读 config 缓存，不发 dws）。"""
        return [o.get("name") for o in
                ((self.tbl.config.get("options") or {}).get("job") or {}).get("work_location", [])
                if o.get("name")]

    # -- 读路径 ---------------------------------------------------------------
    def scan_jobs(self, scan_fields: Sequence[str]) -> List[Dict[str, Any]]:
        """阶段 2 全表扫描（复合键查重 + 岗位ID 最大序号一次拿全，省一次调用）。"""
        return self.tbl.query_records("job", fields=list(scan_fields), all_pages=True,
                                      max_pages=100)

    def query_by_names(self, names: Sequence[str],
                       fields: Sequence[str]) -> List[Dict[str, Any]]:
        """--apply 的 job_name → record_id 补查（≤100 名/片）。"""
        recs: List[Dict[str, Any]] = []
        for i in range(0, len(names), 100):
            recs.extend(self.tbl.query_records("job", filter={"job_name": names[i:i + 100]},
                                               fields=list(fields),
                                               limit=100, all_pages=True))
        return recs

    # -- 选项池 / 当前用户 ------------------------------------------------------
    def ensure_options(self, field_key: str, names: Sequence[str]) -> Any:
        return self.tbl.ensure_options("job", field_key, names)

    def fetch_current_user_cell(self, warnings: List[str]
                                ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """需求提交人 = 当前登录用户（可选）。

        一次 `dws contact +me` **只读**调用拿 userId，拼成 user 字段的写入格式
        `[{"userId": "..."}]`（dws aitable record create 帮助文档的 user 类型格式）。
        获取失败不致命：返回 (None, None)，该列留空并进 warnings。
        """
        try:
            res = self.tbl.client.call(["contact", "+me"], yes=False)
            # aitable.client.unwrap 已统一识别 `+` 命令的双层信封 {"ok","outcome","data"}，
            # 这里拿到的 res["data"] 就是内层业务数据。
            d = (res or {}).get("data") or {}
            uid = d.get("userId") or d.get("user_id")
            if not uid:
                warnings.append("dws contact +me 未返回 userId（实得 %s）→ 需求提交人本次留空"
                                % json.dumps(d, ensure_ascii=False)[:120])
                return None, None
            cell: Dict[str, Any] = {"userId": str(uid)}
            corp = d.get("corpId") or d.get("corp_id")
            if corp:
                cell["corpId"] = str(corp)
            return [cell], (d.get("name") or None)
        except Exception as exc:
            warnings.append("获取当前登录用户失败（%s: %s）→ 需求提交人本次留空，"
                            "可重跑或人工补填" % (type(exc).__name__, str(exc)[:160]))
            return None, None

    # -- 写路径 ---------------------------------------------------------------
    def upload_attachments(self, paths: Sequence[str], concurrency: int) -> Any:
        return self.tbl.upload_attachments(list(paths), concurrency=concurrency)

    def batch_create(self, rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        return self.tbl.batch_create("job", list(rows))

    def batch_update(self, payload: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        return self.tbl.batch_update("job", list(payload))

    def batch_update_verified(self, updates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """安全回填路径：≤10 条/片 + 回读 + 有界重试（不空转烧调用）。"""
        return self.tbl.batch_update_verified("job", updates, chunk_size=10, settle_tries=2)

    def readback_verify(self, lag_ids: Sequence[str], fields: Sequence[str],
                        expected: Dict[str, Any]) -> Dict[str, Any]:
        """写入传播延迟的迟到二次复核（1 次调用，不空转）。"""
        return self.tbl.readback_verify("job", lag_ids, fields,
                                        expected=expected, settle_tries=1)
