# -*- coding: utf-8 -*-
"""decisions 上下文构建（DecisionContextBuilder）：digest 优先，缺 digest 时
从表里/decisions 里降级解析。

JobRecordParser 由构造注入。

已知现状（**不显式授权不要修**，原样保留）：
synth_digest_from_table 会**就地改写 decisions 的 job_key**。
"""

from typing import Any, Dict, List, Optional

from aitable.client import DwsError
from aitable.schema import AITableConfigError
from aitable.table import AITable

from match.applyvalues import as_text
from match.match_basics import JOB_STATUS_OPEN


def resolve_job(job_index: Dict[str, Dict[str, Any]], entry: Dict[str, Any],
                jk: Optional[str]) -> Optional[Dict[str, Any]]:
    for cand in (jk, entry.get("job_id") and "job_id:%s" % entry.get("job_id"),
                 entry.get("job_name") and "job_name:%s" % entry.get("job_name")):
        if cand and cand in job_index:
            return job_index[cand]
    return None


class DecisionContextBuilder:
    """job/candidate 两级索引 + 无 digest 时的表侧降级合成。"""

    def __init__(self, job_parser: Any):
        self._parser = job_parser

    def rows_needed(self, decisions: Dict[str, Any]) -> bool:
        """decisions 里有没有需要落库的 pass 条目（决定「查不到候选人」是不是致命）。"""
        return bool([p for p in (decisions.get("passed") or []) if isinstance(p, dict)])

    def synth_digest_from_table(self, table: AITable, decisions: Dict[str, Any],
                                warnings: List[str]) -> Dict[str, Any]:
        """没传 --digest 时的降级路径：从**表里**现查岗位，拼一个够 verify 用的最小 digest。

        verify 真正需要的是岗位的 `must_skills / bonus_skills / weights`（算 ⊆ 与分数）
        和候选人的 `key / org_guess`（算覆盖率）。组织归属在这里不可靠，所以调用方必须
        用 `check_coverage=False`。
        """
        jobs: List[Dict[str, Any]] = []
        alias: Dict[str, str] = {}
        try:
            recs = table.query_records("job", filter={"status": JOB_STATUS_OPEN}, all_pages=True)
        except (DwsError, AITableConfigError) as exc:
            warnings.append("没传 --digest 且查在招岗位失败（%s）→ 无法做集合/算术校验" % str(exc)[:160])
            recs = []
        for i, r in enumerate(recs, 1):
            cells = r.get("cells") or {}
            j = self._parser.parse(cells, as_text(cells.get("job_name")))
            j["record_id"] = r.get("record_id")
            j["key"] = "j%02d" % i
            jobs.append(j)
            if j.get("job_id"):
                alias["job_id:%s" % j["job_id"]] = j["key"]
            if j.get("job_name"):
                alias["job_name:%s" % j["job_name"]] = j["key"]
        # decisions 里引用的 job_key 如果本身不是 j01..jNN，就用别名映射改写成一个存在的 key
        renamed = 0
        for e in (decisions.get("passed") or []) + (decisions.get("rejected") or []):
            if not isinstance(e, dict):
                continue
            if e.get("job_key") and e["job_key"] not in {j["key"] for j in jobs} \
                    and e["job_key"] in alias:
                e["job_key"] = alias[e["job_key"]]
                renamed += 1
            jks = e.get("job_keys")
            if isinstance(jks, list):
                new = []
                for jk in jks:
                    if str(jk) not in {j["key"] for j in jobs} and str(jk) in alias:
                        new.append(alias[str(jk)])
                        renamed += 1
                    else:
                        new.append(jk)
                e["job_keys"] = new
        if renamed:
            warnings.append("没传 --digest：%d 个 job_key 用 job_id/岗位名称 从表里映射回了岗位 key"
                            % renamed)
        keys = set()
        for e in (decisions.get("passed") or []) + (decisions.get("rejected") or []):
            if isinstance(e, dict) and e.get("candidate_key"):
                keys.add(str(e["candidate_key"]))
        cands = [{"key": k, "name": None, "org_guess": None, "evidence": {}} for k in sorted(keys)]
        return {"batch_id": decisions.get("batch_id"), "candidates": cands, "jobs": jobs,
                "synthesized_from_table": True}

    def build_job_index(self, table: Optional[AITable], digest: Optional[Dict[str, Any]],
                        warnings: List[str]) -> Dict[str, Dict[str, Any]]:
        """{job_key: job}。digest 缺失时按 job_id / job_name 从表里查在招岗位重建索引。"""
        if digest and isinstance(digest.get("jobs"), list) and digest["jobs"]:
            return {str(j["key"]): j for j in digest["jobs"] if isinstance(j, dict) and j.get("key")}
        if table is None:
            return {}
        warnings.append("没有 --digest：岗位信息改从表里查（job_key 需要 decisions 里带 "
                        "job_id 或 job_name 才能对上）")
        recs = table.query_records("job", filter={"status": JOB_STATUS_OPEN}, all_pages=True)
        idx: Dict[str, Dict[str, Any]] = {}
        for i, r in enumerate(recs):
            cells = r.get("cells") or {}
            j = self._parser.parse(cells, as_text(cells.get("job_name")))
            j["record_id"] = r.get("record_id")
            j["key"] = "j%02d" % (i + 1)
            idx[j["key"]] = j
            if j.get("job_id"):
                idx["job_id:%s" % j["job_id"]] = j
            if j.get("job_name"):
                idx["job_name:%s" % j["job_name"]] = j
        return idx

    def build_candidate_index(self, table: Optional[AITable], digest: Optional[Dict[str, Any]],
                              decisions: Dict[str, Any],
                              warnings: List[str]) -> Dict[str, Dict[str, Any]]:
        """{candidate_key: candidate}。digest 缺失时退化成「decisions 里带的 name/phone」。"""
        idx: Dict[str, Dict[str, Any]] = {}
        if digest and isinstance(digest.get("candidates"), list):
            for c in digest["candidates"]:
                if isinstance(c, dict) and c.get("key"):
                    idx[str(c["key"])] = dict(c)
        for e in (decisions.get("passed") or []) + (decisions.get("rejected") or []):
            if not isinstance(e, dict):
                continue
            ck = e.get("candidate_key")
            if ck is None:
                continue
            ck = str(ck)
            slot = idx.setdefault(ck, {"key": ck})
            for f in ("name", "candidate_name", "phone", "record_id", "org", "org_guess"):
                if e.get(f) and not slot.get(f):
                    slot["org_guess" if f == "org" else f] = e[f]
        if not digest:
            warnings.append("没有 --digest：候选人档案信息只能从 decisions/表里取，"
                            "写入匹配记录的「候选人技能/期望职位/工作年限」可能不全")
        return idx
