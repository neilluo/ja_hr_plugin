# -*- coding: utf-8 -*-
"""TableGateway：intake_resume.py 的 dws IO 边界唯一入口（P7 刀4）。

收拢原散在 run() 里的 dws 装配与**写路径**调用站点：
  装配          counter/client/tbl 三件套 + config 失败 fatal（tbl=None 继续跑）
  reset         --reset 全表 query + batch_delete（原模块级 reset_table 逐字搬移）
  ensure_options 选项池 ensure 写（只增不删、保留已有选项 id 一并回传的语义
                在 aitable/optionpool.py，本层只转发、不改 settle_tries 默认）
  upload_attachments 附件上传（阶段6 逐片 / 阶段6b 补传两处共用；**不复用
                fileToken**、重传语义在 aitable/uploader.py，本层不改）
  batch_update / batch_upsert_by_key 记录 create/update（upsert 批）
  set_row_count 把去重扫描**免费**拿到的行数喂给写前护栏

红线（垫片 argv 序列是主裁判，逐条不变）：
  * dws 调用的**顺序与次数**：本类全部方法是薄转发，参数组装逐字保持原调用形状；
    `set_row_count` 丢一行 → RecordWriter.guard_row_limit 自己 fetch → **+1 次调用**，
    调用点（scan.truncated 的 else 分支）与循环边界留在 run()，本类不搬判定。
  * 判定逻辑不进本类：去重扫描（lib_deduper.scan / phone_deduper.scan 仍由 run()
    拿 `tbl` 直调 deduper）、欠传 fixup 名单、批切分策略、异常归类（DwsError 的
    except 分支与 warning/fatal 文本）全部留在 run()。
  * `field_keys` 不发 dws（读 TableSchema 的 config 缓存），**不得**「优化」成实时查询。
  * 回读校验（verify_by_filter / 阶段6b 附件轮询 query）是刀5 ReadBackVerifier 的
    范围，本类不收；本类也**不新增**任何 checkpoint 落盘时机。

与 shared/aitable/ 的关系：aitable 包（P2 立的层）是 dws 的实现层，本类是
**入口编排与 aitable 包之间的适配层**——不塞进 aitable 包、不改 aitable 包。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from aitable.client import DwsCallCounter, DwsClient
from aitable.table import AITable


class TableGateway:
    """dws IO 边界。`tbl` 可为 None——语义是「config 失败但不致命」（fatal 已给出，
    run() 继续跑完流程产出完整报告），全流程 20+ 处 `if tbl is not None` 门控据此判定。"""

    def __init__(self, config_path: str) -> None:
        self.fatal: Optional[str] = None
        self.counter = DwsCallCounter()
        self.client = DwsClient(counter=self.counter, timeout=300, http_timeout=180)
        try:
            self.tbl: Optional[AITable] = AITable(config_path, client=self.client)
        except Exception as exc:                  # config 缺失/格式错 → 立刻可见地失败
            self.fatal = "读取 config.json 失败：%s: %s" % (type(exc).__name__, exc)
            self.tbl = None

    # -- 只读（config 缓存，不发 dws） -------------------------------------
    def field_keys(self, table_key: str) -> List[str]:
        return self.tbl.field_keys(table_key)

    # -- --reset（仅供重复测量；生产 base 严禁使用） -------------------------
    def reset(self, table_key: str) -> Dict[str, Any]:
        """清空表内全部记录（原 intake_resume.reset_table 逐字搬移）。"""
        recs = self.tbl.query_records(table_key, fields=["phone"], all_pages=True,
                                      max_pages=100)
        ids = [r["record_id"] for r in recs if r.get("record_id")]
        if not ids:
            return {"deleted": 0, "failed": [], "found": 0}
        res = self.tbl.batch_delete(table_key, ids)
        res["found"] = len(ids)
        return res

    # -- 选项池 ensure 写 ----------------------------------------------------
    def ensure_options(self, table_key: str, field_key: str,
                       names: Sequence[str]) -> List[Dict[str, Any]]:
        return self.tbl.ensure_options(table_key, field_key, names)

    # -- 附件上传 ------------------------------------------------------------
    def upload_attachments(self, file_paths: Sequence[str],
                           concurrency: int) -> List[Dict[str, Any]]:
        return self.tbl.upload_attachments(file_paths, concurrency=concurrency)

    # -- 记录写（upsert 批 / update 批） -------------------------------------
    def batch_update(self, table_key: str,
                     updates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        return self.tbl.batch_update(table_key, updates)

    def batch_upsert_by_key(self, table_key: str, unique_field: str,
                            rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        return self.tbl.batch_upsert_by_key(table_key, unique_field, rows)

    # -- 写前护栏喂行数（不发 dws；省掉一次 record stats） --------------------
    def set_row_count(self, table_key: str, count: int) -> None:
        self.tbl.set_row_count(table_key, count)
