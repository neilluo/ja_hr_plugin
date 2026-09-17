# -*- coding: utf-8 -*-
"""候选人侧归一（CandidateNormalizer）：candidates.json 元素 → digest 元素。

原 build_match_input.py 的 `build_evidence` / `evidence_truncated` /
`_flat_window` / `enrich_evidence` / `years_source_of` / `normalize_candidate`
（L470-664）搬入本类；`aggregate_identity_review` 收编原 build_digest 里的
P5 身份复核聚合段（L1285-1314，逐字）。

身份安全阀判据来自 shared/fields/identity.py（P5，纯函数）——与 intake 侧共用
的是**判据**，不是 guess_org（build 侧导出 org_confidence=low 仍写库的策略与
intake 侧相反，禁止合并，见 P8 报告红线）。

就地语义说明：enrich_evidence / aggregate_identity_review 会就地写候选人 dict
（与原实现逐字一致）。这些 dict 是 normalize() 新造的编排层自有对象，不是外部
调用方输入，就地修改被编排层封闭（分析报告 D 节原则 3 的「返回新对象」化留给
后续显式授权的刀，本刀红线是行为逐字节不变）。
"""

import re
from typing import Any, Dict, List, Optional

from fields.identity import (           # P5 身份安全阀判据（纯函数，零第三方依赖）
    IDENTITY_EVIDENCE_KEYS,
    IDENTITY_LINE_LIMIT,
    email_ocr_noise,
    identity_evidence,
    name_review_reason,
)
from match.constants import DEFAULT_LOCATION
from match.tablevalues import WORK_TEXT_LIMIT, SKILL_TEXT_LIMIT, as_list, \
    clean_ws, clip, dedupe_keep_order, full

#: 契约 §3.3 candidates 元素的字段清单（只增不减：C1 多给的字段原样透传）
#: P5 只增：email（身份阀的判据与原文面要用）/ name_source / parse_backend（姓名复核判据）
CANDIDATE_PASS_THROUGH = (
    "record_id", "file_name", "name", "phone", "email", "education", "school",
    "school_rank", "major", "years_experience", "certificates", "skills",
    "expected_position", "expected_location", "expected_salary",
    "org_guess", "org_confidence", "category_guess",
    "parse_status", "dedupe", "attachment_status",
    "name_source", "parse_backend",
)

# --- evidence 兜底：W-A 的分段是按标题正则切的，实测 17 份真实简历里有 6 份切不出
#     「教育经历」段、2 份切不出「工作经历」段（简历根本没有这些标题）。D4 的坑
#     （许金×财务主管误杀、代文超×单晶生产主管漏判）正是「教育/证书段丢了」造成的，
#     所以段落为空时**必须**从简历全文里按关键词开窗兜底，而不是给 LLM 一个空字符串。
_EDU_HINT = re.compile(
    r"(学历|教育(?:经历|背景|情况|程度)?|毕业(?:院校|时间|学校)?|全日制|"
    r"大学本科|本科|大专|硕士|博士|学士|大学|学院)")
_CERT_HINT = re.compile(
    r"(证\s*书|资格证书|职业资格|职称|执业|注册[\u4e00-\u9fff]{0,6}(?:师|证)|"
    r"特种作业|电工证|会计师|驾驶证)")
_WORK_HINT = re.compile(
    r"(工作(?:经历|经验|履历)|职业经历|从业经历|任职经历|项目(?:经历|经验|描述)|"
    r"实习经历|主要工作|履历)")
_SKILL_HINT = re.compile(r"(专业技能|个人技能|技能特长|技能技巧|技术能力|专业能力|核心能力|技能)")


class CandidateNormalizer:
    """candidates.json 元素 → 契约 §3.3 digest candidates 元素（纯计算）。"""

    def build_evidence(self, cand: Dict[str, Any]) -> Dict[str, str]:
        """D4：education_text / cert_text **完整保留**；work_text / skill_text 可截断。

        兼容 C1 把原文段放在 `evidence` 里或放在 `sections` 里（W-A extract_fields 用 `sections`）。
        P5 只增：身份字段原文行 name_text / email_text / location_text（≤60 字，判据见
        shared/fields/identity.py）——上游给了就透传（再封顶一次），没给先置空串，
        有全文时由 enrich_evidence 兜底补。
        """
        src = cand.get("evidence")
        if not isinstance(src, dict):
            src = cand.get("sections") if isinstance(cand.get("sections"), dict) else {}
        edu = full(src.get("education_text"))
        cert = full(src.get("cert_text"))
        work = clip(src.get("work_text"), WORK_TEXT_LIMIT)
        skill = clip(src.get("skill_text"), SKILL_TEXT_LIMIT)
        if not (edu or cert or work or skill):
            # 兜底：C1 只给了简历全文
            ft = full(cand.get("full_text") or cand.get("resume_text"))
            work = clip(ft, WORK_TEXT_LIMIT)
        ev = {"education_text": edu, "cert_text": cert, "work_text": work, "skill_text": skill}
        for k in IDENTITY_EVIDENCE_KEYS:
            ev[k] = clip(clean_ws(src.get(k)), IDENTITY_LINE_LIMIT, "…")
        return ev

    def evidence_truncated(self, cand: Dict[str, Any], ev: Dict[str, str]) -> Dict[str, bool]:
        src = cand.get("evidence")
        if not isinstance(src, dict):
            src = cand.get("sections") if isinstance(cand.get("sections"), dict) else {}
        return {
            "education_text": len(full(src.get("education_text"))) != len(ev["education_text"]),
            "cert_text": len(full(src.get("cert_text"))) != len(ev["cert_text"]),
            "work_text": len(full(src.get("work_text"))) != len(ev["work_text"]),
            "skill_text": len(full(src.get("skill_text"))) != len(ev["skill_text"]),
        }

    def flat_window(self, text: str, hint: "re.Pattern", before: int = 40, after: int = 340) -> str:
        """在全文里找关键词，取一个压缩过空白的窗口。找不到返回 ""。"""
        if not text:
            return ""
        flat = re.sub(r"\s+", " ", text)
        m = hint.search(flat)
        if not m:
            return ""
        st = max(0, m.start() - before)
        return flat[st:m.start() + after].strip()

    def enrich_evidence(self, cand: Dict[str, Any], full_text: str) -> List[str]:
        """段落为空时用简历全文兜底补 evidence。返回本次兜底的说明（进 warnings）。

        **就地**写 cand["evidence"] / cand["evidence_provenance"] / cand["evidence_chars"]
        （与原实现逐字一致；cand 是编排层自有对象）。
        """
        notes: List[str] = []
        ev = cand.get("evidence") or {}
        prov = cand.setdefault("evidence_provenance", {})
        for k in ("education_text", "cert_text", "work_text", "skill_text"):
            prov[k] = "section" if (ev.get(k) or "").strip() else "empty"
        ft = full_text[:20000] if full_text else ""
        if not ft:
            return notes
        plan = (("education_text", _EDU_HINT, 40, 340, WORK_TEXT_LIMIT),
                ("cert_text", _CERT_HINT, 40, 260, WORK_TEXT_LIMIT),
                ("work_text", _WORK_HINT, 20, WORK_TEXT_LIMIT, WORK_TEXT_LIMIT),
                ("skill_text", _SKILL_HINT, 20, SKILL_TEXT_LIMIT, SKILL_TEXT_LIMIT))
        for key, hint, before, after, limit in plan:
            if (ev.get(key) or "").strip():
                continue
            win = self.flat_window(ft, hint, before, after)
            if not win:
                continue
            # 教育/证书段（D4）：兜底窗口也**不截断**；work/skill 段按各自上限截断
            ev[key] = win if key in ("education_text", "cert_text") else clip(win, limit)
            prov[key] = "full_text_fallback"
            notes.append("%s(%s)：evidence.%s 上游分段为空，已从简历全文按关键词兜底开窗 %d 字"
                         % (cand.get("name") or "?", cand.get("key"), key, len(ev[key])))
        if not (ev.get("work_text") or "").strip():
            # 连关键词都找不到 → 直接给正文头部（总比空字符串强）
            head = clip(re.sub(r"\s+", " ", ft), WORK_TEXT_LIMIT)
            if head:
                ev["work_text"] = head
                prov["work_text"] = "full_text_head"
                notes.append("%s(%s)：evidence.work_text 兜底也找不到段落标题，改取简历全文头部 %d 字"
                             % (cand.get("name") or "?", cand.get("key"), len(head)))
        # P5：身份原文行兜底——旧版 candidates.json / 模式 B 可能没带 name_text 等三键，
        # 有全文就按同一套判据补上（命中行原文；姓名未命中取抬头行；地点未命中留空不标记）。
        if any(not (ev.get(k) or "").strip() for k in IDENTITY_EVIDENCE_KEYS):
            loc_raw = cand.get("expected_location")
            if clean_ws(loc_raw) == DEFAULT_LOCATION:
                loc_raw = None            # 「不限」是 D14 兜底值，不是原文词，不拿它去搜行
            lines = identity_evidence(ft, cand.get("name"), cand.get("email"), loc_raw)
            for k in IDENTITY_EVIDENCE_KEYS:
                if not (ev.get(k) or "").strip() and lines.get(k):
                    ev[k] = lines[k]
                    prov[k] = "full_text_fallback"
        cand["evidence"] = ev
        cand["evidence_chars"] = sum(len(v or "") for v in ev.values())
        return notes

    def years_source_of(self, cand: Dict[str, Any]) -> Optional[str]:
        """W-A 输出的键名是 `years_experience_source`，契约 D13 写的是 `years_source`，两个都认。"""
        for k in ("years_source", "years_experience_source"):
            v = cand.get(k)
            if isinstance(v, str) and v:
                return v
        conf = cand.get("confidence")
        if isinstance(conf, dict) and conf.get("years_experience") == "estimated":
            return "estimated"
        return None

    def normalize(self, cand: Dict[str, Any], idx: int, warnings: List[str]) -> Dict[str, Any]:
        """把 C1 的 candidates.json 元素归一成契约 §3.3 的 digest candidates 元素。"""
        key = clean_ws(cand.get("key")) or "c%02d" % (idx + 1)
        out: Dict[str, Any] = {"key": key}
        for f in CANDIDATE_PASS_THROUGH:
            if f in cand:
                out[f] = cand[f]
        out.setdefault("record_id", cand.get("record_id"))
        out.setdefault("name", cand.get("name"))
        out.setdefault("phone", cand.get("phone"))

        # 列表字段归一
        for f in ("certificates", "skills"):
            out[f] = dedupe_keep_order(as_list(cand.get(f)))

        # D14：期望地点缺失 → 兜底「不限」
        loc = clean_ws(cand.get("expected_location"))
        if not loc:
            loc = DEFAULT_LOCATION
            warnings.append("%s(%s)：期望地点缺失，按老插件铁律兜底填「不限」"
                            "（agent 若从 evidence 看出明确城市请在 candidate_overrides 覆盖）"
                            % (out.get("name") or "?", key))
        out["expected_location"] = loc

        # D13：估计出来的工作年限必须标 needs_review
        ysrc = self.years_source_of(cand)
        out["years_source"] = ysrc
        needs = dedupe_keep_order(as_list(cand.get("needs_review")))
        if ysrc == "estimated" and "years" not in needs:
            needs.append("years")
            warnings.append("%s(%s)：工作年限 %s 是脚本估算的（years_source=estimated），"
                            "已标 needs_review=[years]；agent 必须用 evidence 原文复核后在 "
                            "candidate_overrides.years_experience 回填，不要直接当硬门槛用"
                            % (out.get("name") or "?", key, out.get("years_experience")))

        # P5 身份安全阀（判据在 shared/fields/identity.py）：姓名来源 filename/OCR/agent 草稿
        # → needs_review 追加 "name"；邮箱命中 OCR 噪声规则 → 追加 "email"。只标记不拦截
        # （尺度同 D13）；逐人 warning 会按分片数复制、撑大 shard 体积，这里把判据记在
        # 临时键 _p5_identity_notes 上，由 aggregate_identity_review 汇成聚合 warning 后弹出。
        p5_notes: List[str] = []
        n_reason = name_review_reason(cand.get("name_source"), cand.get("parse_backend"),
                                      cand.get("field_sources"))
        if n_reason and clean_ws(out.get("name")) and "name" not in needs:
            needs.append("name")
            p5_notes.append("name：%s" % n_reason)
        e_reason = email_ocr_noise(out.get("email"))
        if e_reason and "email" not in needs:
            needs.append("email")
            p5_notes.append("email：%s" % e_reason)
        if p5_notes:
            out["_p5_identity_notes"] = p5_notes
        out["needs_review"] = needs

        # evidence（D4）
        ev = self.build_evidence(cand)
        out["evidence"] = ev
        trunc = self.evidence_truncated(cand, ev)
        if trunc["education_text"] or trunc["cert_text"]:
            # 理论上不可能：build_evidence 对这两段不截断。真发生说明 C1 给的就是截断值。
            warnings.append("%s(%s)：education_text/cert_text 被上游截断，违反 D4，"
                            "门槛判定可能误杀/漏判" % (out.get("name") or "?", key))
        out["evidence_truncated"] = trunc
        out["evidence_chars"] = sum(len(v) for v in ev.values())

        # 透传 C1 的其余只增字段（years_experience_est / confidence / warnings 等）
        for k, v in cand.items():
            if k not in out and k not in ("evidence", "sections", "full_text", "resume_text"):
                out[k] = v
        return out

    def aggregate_identity_review(self, active: List[Dict[str, Any]],
                                  warnings: List[str]) -> None:
        """P5：身份复核聚合 warning（逐人判据在 normalize 的临时键里；聚合是
        为了控制分片体积——meta.warnings 会按分片数复制。复核动作由 needs_review +
        evidence.name_text/email_text 驱动，消费面规则见 HOTPATH.md 回合 2）。
        原 build_digest L1285-1314 逐字搬入（就地弹 _p5_identity_notes、按需追加
        needs_review+="email"、往 warnings 追加两条聚合文案）。"""
        p5_name_flagged: List[str] = []
        p5_email_flagged: List[str] = []
        for c in active:
            notes = list(c.pop("_p5_identity_notes", None) or [])
            # 旧版 candidates.json 不带 email：check_onboarded 在 normalize **之后**才从表里
            # 回补，邮箱噪声阀对回补值再判一次（新版 C1 已在 normalize 里判过，去重不重复标）
            e_reason = email_ocr_noise(c.get("email"))
            if e_reason and "email" not in as_list(c.get("needs_review")):
                c["needs_review"] = dedupe_keep_order(as_list(c.get("needs_review")) + ["email"])
                notes.append("email：%s" % e_reason)
            for note in notes:
                label = "%s(%s)" % (c.get("name") or "?", c.get("key"))
                if note.startswith("name"):
                    p5_name_flagged.append(label)
                else:
                    p5_email_flagged.append(label)
        if p5_name_flagged:
            warnings.append("P5 姓名待复核 %d 人（来源=文件名/OCR/agent 草稿，已标 needs_review+"
                            "name）：%s%s；Turn 2 必须对照 evidence.name_text 原文复核，"
                            "误读要业务话照转请用户人工修正"
                            % (len(p5_name_flagged), "、".join(p5_name_flagged[:10]),
                               " 等" if len(p5_name_flagged) > 10 else ""))
        if p5_email_flagged:
            warnings.append("P5 邮箱疑似 OCR 噪声 %d 人（域名无点/TLD 含非字母/域名主体字母"
                            "数字混排如 qq.com→q9.com，已标 needs_review+email）：%s；"
                            "Turn 2 必须对照 evidence.email_text 原文复核并照转"
                            % (len(p5_email_flagged), "、".join(p5_email_flagged[:10])))
