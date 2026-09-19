# -*- coding: utf-8 -*-
"""表/文件侧读取边界（MatchSourceGateway）：build 侧唯一持 AITable 的 IO 类。

两条排序稳定性铁律（分片可复现性的根，方法内注释保留原文）：
  * fetch_open_jobs：jobs 按 job_id → job_name → key 稳定排序后**重编号**；
  * fetch_candidates_from_table：recs 按 姓名 → record_id 稳定排序。

check_onboarded **就地**校正 candidates（表是唯一事实源）。
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from aitable.client import DwsError
from aitable.schema import AITableConfigError
from aitable.table import AITable

from match.match_basics import COMM_STATUS_ONBOARDED, JOB_STATUS_OPEN
from match.match_basics import strip_md_fence
# _LateBoundExtractor：入口注入的「调用时查名」代理（保持原模块级条件 import 的全局查找时机）；isinstance 判定与之配套。
from match.jobparse import _LateBoundExtractor
from match.tablevalues import WORK_TEXT_LIMIT, SKILL_TEXT_LIMIT, as_list, \
    as_number, as_text, clean_ws, clip, dedupe_keep_order, full


def _extractor_available(ex: Any) -> bool:
    if isinstance(ex, _LateBoundExtractor):
        return ex.is_available()
    return ex is not None


class MatchSourceGateway:
    """岗位/候选人取数（IO 边界）。job_parser 由编排层注入（含降级语义）；
    resume_field_extractor = shared/extract_fields.extract_resume_fields，可为 None
    （--from-table 切 evidence 用；缺失时降级）。"""

    def __init__(self, table: AITable, job_parser: Any,
                 resume_field_extractor: Any = None):
        self.table = table
        self._parser = job_parser
        self._extract_resume_fields = resume_field_extractor

    def load_candidates_file(self, path: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """吃 C1 产出的 candidates.json。也容忍直接给一个 candidates 数组。"""
        if not path.exists():
            raise SystemExit("candidates.json 不存在：%s\n"
                             "（它由 resume-intake 的 intake_resume.py 产出；"
                             "请先跑简历入库，或用 --candidates 指向正确的绝对路径）" % path)
        with open(str(path), "r", encoding="utf-8") as fh:
            raw = fh.read()
        body = strip_md_fence(raw.strip())
        try:
            data = json.loads(body)
        except Exception as exc:
            raise SystemExit("candidates.json 不是合法 JSON：%s: %s" % (type(exc).__name__, exc))
        if isinstance(data, list):
            return data, {}
        if not isinstance(data, dict):
            raise SystemExit("candidates.json 结构不认识（既不是 dict 也不是 list）")
        cands = data.get("candidates")
        if not isinstance(cands, list):
            raise SystemExit("candidates.json 缺 candidates 数组")
        return cands, data

    def fetch_open_jobs(self, warnings: List[str]) -> List[Dict[str, Any]]:
        """从**表里**查在招岗位（不依赖 jobs_draft.json）。"""
        try:
            recs = self.table.query_records("job", filter={"status": JOB_STATUS_OPEN},
                                            all_pages=True)
        except (DwsError, AITableConfigError) as exc:
            raise SystemExit("查在招岗位失败：%s\n"
                             "（检查 config.json 里 job 表的 status 字段映射，"
                             "以及 base 是否可访问）" % exc)
        jobs = []
        for i, r in enumerate(recs):
            cells = r.get("cells") or {}
            j = self._parser.parse(cells, clean_ws(cells.get("job_name")))
            j["record_id"] = r.get("record_id")
            j["key"] = "j%02d" % (i + 1)
            if not j["must_skills"]:
                warnings.append("岗位 %s(%s)：必备技能解析为空 → 技能得分分母为 0，"
                                "该岗位无法打分（请检查 job 表「必备技能」字段）"
                                % (j["job_name"], j["key"]))
            jobs.append(j)
        # 排序稳定：按 job_id → job_name → key（避免 set/dict 迭代序影响分片可复现性）
        jobs.sort(key=lambda x: (x.get("job_id") or "", x.get("job_name") or "", x["key"]))
        for i, j in enumerate(jobs):                      # 排序后重编号，保证 key 连续可读
            j["key"] = "j%02d" % (i + 1)
        return jobs

    def check_onboarded(self, candidates: List[Dict[str, Any]],
                        warnings: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """老插件铁律：沟通状态=已入职 → 不参与匹配。

        以**表**为准（不是 candidates.json），1 次 `record query --record-ids` 拿回 comm_status，
        顺手用表里的 org / name / phone 校正 candidates.json（表是唯一事实源，**就地**改）。
        """
        ids = [c.get("record_id") for c in candidates if c.get("record_id")]
        if not ids:
            warnings.append("候选人没有 record_id，无法核对「沟通状态」，本批全部按参与处理")
            return candidates, []
        try:
            recs = self.table.query_records("resume", record_ids=ids,
                                            fields=["name", "phone", "comm_status", "org", "category",
                                                    "email", "full_text"])
        except (DwsError, AITableConfigError) as exc:
            warnings.append("核对「沟通状态」失败（%s），本批全部按参与处理" % str(exc)[:160])
            return candidates, []
        by_id = {r.get("record_id"): (r.get("cells") or {}) for r in recs}
        active, onboarded = [], []
        for c in candidates:
            cells = by_id.get(c.get("record_id"))
            if cells:
                cs = clean_ws(as_text(cells.get("comm_status")))
                c["comm_status"] = cs or None
                tbl_org = clean_ws(as_text(cells.get("org"))) or None
                if not c.get("org_guess"):
                    c["org_guess"] = tbl_org
                elif tbl_org and tbl_org != clean_ws(c.get("org_guess")):
                    # 表是唯一事实源——apply_decisions 写回组织改判（或用户人工改库）后，
                    # 重跑同一命令必须按**表里的新组织**重切预筛与组合。改动显式报告。
                    warnings.append("%s(%s)：简历库组织=%s 与 candidates.json 的 org_guess=%s "
                                    "不一致 → 以库内为准（表是唯一事实源），本轮组织预筛与组合"
                                    "按库内值重切"
                                    % (c.get("name") or "?", c.get("key"), tbl_org,
                                       c.get("org_guess")))
                    c["org_guess"] = tbl_org
                if not c.get("category_guess"):
                    c["category_guess"] = clean_ws(as_text(cells.get("category"))) or None
                if not c.get("email"):
                    # 邮箱身份阀要在 C2 判 OCR 噪声；旧版 candidates.json 没带 email 时
                    # 从表里回补（同一次查询顺带取，零额外调用）
                    c["email"] = clean_ws(as_text(cells.get("email"))) or None
                ft = as_text(cells.get("full_text"))
                if ft:
                    c["_full_text_from_table"] = ft
                if cs == COMM_STATUS_ONBOARDED:
                    onboarded.append({"key": c.get("key"), "name": c.get("name"),
                                      "record_id": c.get("record_id"),
                                      "reason": "沟通状态=已入职（老插件铁律：不参与匹配，"
                                                "其系统匹配记录由 apply_decisions 删除）"})
                    continue
            active.append(c)
        missing = [i for i in ids if i not in by_id]
        if missing:
            warnings.append("有 %d 个候选人 record_id 在简历表里读不到（%s...），已按参与处理"
                            % (len(missing), ",".join(str(m) for m in missing[:3])))
        return active, onboarded

    #: --from-table 导出的候选人字段集合：**与 C1 intake_resume.py 产出的 candidates.json
    #: 元素完全同构**。email 也导出（身份阀判据），并补 name_source / parse_backend 两键
    #: （表里不存来源，置 None）保持键集合与模式 A 一致。
    def fetch_candidates_from_table(self, org: Optional[str] = None,
                                    exclude_onboarded: bool = False,
                                    warnings: Optional[List[str]] = None
                                    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """从简历库表批量导出**存量候选人**，映射成与 candidates.json
        完全相同的 candidates 结构（key 用 c01/c02…，record_id 用表里的真实 id）。

        * 一次 filter 查询取全量（≤100/页由 aitable.query 自动翻页）；`--org` 时在服务端过滤。
        * evidence 从「简历全文」text 列取：全文可用 → 用同一套分段规则
          （extract_resume_fields）切；切不出来 → 整段截断进 work_text，
          并在 meta 里标注 evidence_source="full_text_fallback"（后续 enrich_evidence
          还会按关键词开窗补教育/证书段，双保险）。
        * `--exclude-onboarded` 时在查询映射阶段就排除 沟通状态=已入职；不带该参数时
          已入职候选人也会走 check_onboarded 的老插件铁律剔除（进 meta.excluded_onboarded）。
        """
        warns = warnings if warnings is not None else []
        fields = list(self.table.field_keys("resume") or []) or None
        flt = {"org": org} if org else None
        try:
            recs = self.table.query_records("resume", filter=flt, fields=fields, all_pages=True)
        except (DwsError, AITableConfigError) as exc:
            raise SystemExit("从简历库表批量查询存量候选人失败：%s\n"
                             "（检查 config.json 的 resume 表映射与 base 可访问性；"
                             "若本次是「先入库再匹配」，请改用 --candidates 指向 "
                             "intake_resume.py 产出的 candidates.json）" % exc)
        # 稳定排序：姓名 → record_id（保证 key 编号跨次运行可复现）
        recs = sorted(recs, key=lambda r: (as_text((r.get("cells") or {}).get("name")).strip(),
                                           str(r.get("record_id"))))
        out: List[Dict[str, Any]] = []
        n_excluded_onboarded = 0
        ev_sources = {"sections": 0, "full_text_fallback": 0, "no_full_text": 0}
        for r in recs:
            cells = r.get("cells") or {}
            cs = clean_ws(as_text(cells.get("comm_status")))
            if exclude_onboarded and cs == COMM_STATUS_ONBOARDED:
                n_excluded_onboarded += 1
                continue
            name = clean_ws(as_text(cells.get("name"))) or None
            att = cells.get("attachment") or []
            fname = ""
            if isinstance(att, list):
                for a in att:
                    if isinstance(a, dict):
                        fname = as_text(a.get("filename") or a.get("name")).strip()
                        if fname:
                            break
            cand: Dict[str, Any] = {
                "key": "c%02d" % (len(out) + 1),
                "record_id": r.get("record_id"),
                "file_name": fname or ("%s（在库简历）" % name if name else None),
                "name": name,
                "phone": clean_ws(as_text(cells.get("phone"))) or None,
                # 邮箱导出（身份阀要在 C2 判 OCR 噪声 + 取原文行）；表里不存姓名来源
                # 与解析 backend，两键置 None 只为与模式 A 键集合一致
                "email": clean_ws(as_text(cells.get("email"))) or None,
                "education": clean_ws(as_text(cells.get("education"))) or None,
                "school": clean_ws(as_text(cells.get("school"))) or None,
                "school_rank": clean_ws(as_text(cells.get("school_rank"))) or None,
                "major": clean_ws(as_text(cells.get("major"))) or None,
                "years_experience": None,
                "certificates": [],
                "skills": as_list(cells.get("skills")),
                "expected_position": clean_ws(as_text(cells.get("expected_position"))) or None,
                "expected_location": clean_ws(as_text(cells.get("expected_location"))) or None,
                "org_guess": clean_ws(as_text(cells.get("org"))) or None,
                "org_confidence": "high" if clean_ws(as_text(cells.get("org"))) else "low",
                "category_guess": clean_ws(as_text(cells.get("category"))) or None,
                "parse_status": "ok",
                "dedupe": "existing",                # 存量导出，不是本批 new/overwrite/conflict
                "attachment_status": "uploaded" if att else "missing",
                "years_source": None,                # 表里不存来源；估算值复核以 evidence 原文为准
                "name_source": None,
                "parse_backend": None,
                "evidence": {"education_text": "", "cert_text": "", "work_text": "",
                             "skill_text": "",
                             # 身份原文行：有「简历全文」时由 enrich_evidence 统一兜底补
                             "name_text": "", "email_text": "", "location_text": ""},
            }
            y = as_number(cells.get("years_experience"), None)
            if y is not None:
                cand["years_experience"] = int(y) if float(y).is_integer() else y
            certs_text = clean_ws(as_text(cells.get("certificates")))
            if certs_text:
                cand["certificates"] = dedupe_keep_order(
                    [clean_ws(x) for x in re.split(r"[、,，;；\n]+", certs_text) if clean_ws(x)])
            # evidence：表内「简历全文」→ 分段规则；切不出来整段截断 + 标注
            ft = as_text(cells.get("full_text"))
            if ft:
                secs: Optional[Dict[str, Any]] = None
                if _extractor_available(self._extract_resume_fields):
                    try:
                        secs = (self._extract_resume_fields(ft, fname or "") or {}).get("sections") or {}
                    except Exception:
                        secs = None
                if secs and (clean_ws(secs.get("education_text")) or clean_ws(secs.get("cert_text"))):
                    cand["evidence"] = {
                        "education_text": full(secs.get("education_text")),
                        "cert_text": full(secs.get("cert_text")),
                        "work_text": clip(secs.get("work_text"), WORK_TEXT_LIMIT),
                        "skill_text": clip(secs.get("skill_text"), SKILL_TEXT_LIMIT),
                    }
                    ev_sources["sections"] += 1
                else:
                    cand["evidence"]["work_text"] = clip(ft, WORK_TEXT_LIMIT)
                    ev_sources["full_text_fallback"] += 1
                # enrich_evidence 兜底开窗仍可用全文（教育/证书段为空时按关键词补，双保险）
                cand["_full_text_from_table"] = ft
            else:
                ev_sources["no_full_text"] += 1
                warns.append("%s(%s)：简历表里「简历全文」为空 → evidence 无原文可给，"
                             "门槛判定只能靠字段值；建议重新上传该候选人简历原件"
                             % (name or "?", cand["key"]))
            out.append(cand)
        meta = {
            "from_table_resume_records": len(recs),
            "from_table_excluded_onboarded": n_excluded_onboarded,
            "from_table_evidence_sources": ev_sources,
        }
        return out, meta
