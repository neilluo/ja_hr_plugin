# -*- coding: utf-8 -*-
"""评分口径文案（内嵌进 digest 的 SCORING_RULES 大 dict）。

⚠️ 文案与实现两处漂移是已知风险：本 dict 是**文案**，真正实现分数的
verify_decisions.compute_scores / apply_decisions 消费面各有一份口径
（verify L93 注释原文：「与 build_match_input.SCORING_RULES / apply_decisions
三处必须一致」）。P9 抽 ScoreCalculator 时应落到本模块并加一致性测试，
杜绝漂移；在那之前**逐字保留**（键序即 digest 字节，无 sort_keys）。
"""

# ---------------------------------------------------------------------------
# 评分口径原文（内嵌进 digest，让 LLM 不必去读别的文件）
# 抄自老插件 skills/match-verify/SKILL.md 第 4~6 步 与
# skills/recruit-model/references/system-config.md §6「评分与推荐口径」
# ---------------------------------------------------------------------------
SCORING_RULES = {
    "source": "老插件 recruit-match-suite-v0.1.0：skills/match-verify/SKILL.md 步骤 4-6 + "
              "skills/recruit-model/references/system-config.md §6 评分与推荐口径",
    "hard_gates": {
        "items": ["学历", "专业", "经验年限", "证书"],
        "rule": "四项一票否决：任一项不达标 → 该 候选人×岗位 组合进 rejected，"
                "**不创建任何匹配记录、不打分**（这就是省时间的关键：不再「先生成再删」）。",
        "evidence_first": "以岗位 hard_gates 字段为准，requirements_text（任职要求原文）为佐证；"
                          "候选人侧以字段 + evidence 原文段为准。",
        "insufficient": "证据不足以判断某项时**按不达标处理**，reason 写「证据不足:XX」。",
        "certificate_none": "岗位证书要求为「无明确要求」/「无」/空 时，证书项一律视为达标。",
        "certificate_preferred": "岗位 cert_is_preferred_not_required=true（JD 写的是「持证者优先」）时，"
                                 "证书项视为达标，只在加分项里体现。",
        "education_order": "学历达标 = 候选人学历 ≥ 岗位学历要求（博士 > 硕士 > 本科 > 大专）；"
                           "候选人 education 字段缺失但 evidence.education_text 里能看出学历的，以原文为准。",
        "major": "专业达标 = 候选人 major / evidence.education_text 里的专业与岗位 major_req 同大类或相关；"
                 "岗位 major_req 为空或「不限」时一律达标。",
        "years": "经验年限达标 = 候选人 years_experience ≥ 岗位 years_req_min（岗位写「X年以上」取 X）。"
                 "**候选人 needs_review 含 \"years\" 时（该值是脚本估出来的，28 份实测有 9 份），"
                 "必须先用 evidence.work_text / education_text 原文复核，"
                 "复核后的值写进 candidate_overrides[].years_experience，再判门槛。**",
    },
    "skill_score": "技能得分 = 命中必备技能数 / 岗位 must_skills 总数 × 100，四舍五入取整。"
                   "命中 = evidence 原文里有该技能的直接证据（同义词/缩写算命中，"
                   "如 PLC = S7-1200PLC、CAD = AUTO CAD）。",
    "bonus_score": "加分项得分 = 命中加分项数 / 岗位 bonus_skills 总数 × 100，四舍五入取整；"
                   "岗位 bonus_skills 为空列表时记 100。",
    "total_score": "匹配总分 = 技能得分 × weights.must + 加分项得分 × weights.bonus，四舍五入取整。"
                   "权重取岗位表字段（必备技能权重 / 加分项权重），缺省 0.7 / 0.3。",
    "recommend_thresholds": "总分 ≥ 80 → 「推荐」；60~79 → 「待定」；< 60 → 「不推荐」。",
    "empty_denominator": "岗位 must_skills 为空 → 技能得分记 0 并进 warnings（岗位数据有问题，"
                         "需人工确认）；岗位 bonus_skills 为空 → 加分项得分记 100"
                         "（沿用前序实验已验证口径；老插件 system-config §6 要求这种情况"
                         "先与用户确认，本插件取默认值并在 warnings 里提示）。",
    "rounding": "全部四舍五入取整（half-up，不是 python 的 banker's rounding）。",
    "no_fabrication": "禁止编造：skill_hits / bonus_hits 里列出的每一项**必须是岗位 "
                      "must_skills / bonus_skills 列表里的原文条目**，且能在 evidence 里对应到"
                      "简历原文的原词或明显同义词。越界项会被 verify_decisions.py 判为无效。",
    "scores_computed_by_script": "**分数不由模型输出**（契约 D16）：skill_score / bonus_score / "
                                 "total_score / 推荐状态一律由 apply_decisions.py 按上述口径从 "
                                 "skill_hits / bonus_hits / weights 重算。模型只需给 "
                                 "skill_hits / bonus_hits / recommend（recommend 作对照，"
                                 "不一致记 warnings，以脚本重算为准）。",
    "onboarded_rule": "老插件铁律：候选人「沟通状态=已入职」不参与匹配，其现有系统匹配记录一律删除、"
                      "不打分、不推荐。本脚本已把这类候选人从 digest 里剔除（见 meta.excluded_onboarded），"
                      "agent 不需要为他们产出任何决策。",
}
