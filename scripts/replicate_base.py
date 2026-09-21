#!/usr/bin/env python3
"""在新 Base 复制四表结构（表+字段+单选选项），并打印新 config.json 片段。

用法:
    python3 scripts/replicate_base.py <新baseId> [--operator <unionId>]

说明：钉钉 AI 表格无组织级公共表，跨组织分发=新建 Base 后跑本脚本重建结构，
再用输出的 config 片段替换 config.json 的 base_id/tables 段。
权限：应用需 Notable.Base.Write.All；operator 需对该 Base 有编辑权限。
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from notable import Notable, NotableError  # noqa: E402

# 表结构 = 业务键: (中文字段名, 类型, [单选/多选选项])；与 config.json 的 types/fields 对齐
SCHEMA = {
    "resume": [
        ("name", "姓名", "text", []), ("phone", "手机号", "telephone", []),
        ("email", "邮箱", "email", []),
        ("education", "最高学历", "singleSelect", ["博士", "硕士", "本科", "大专"]),
        ("school", "院校", "text", []),
        ("school_rank", "院校排名", "singleSelect", ["985", "211", "双一流", "普通本科", "大专"]),
        ("years_experience", "工作年限", "number", []), ("major", "专业", "text", []),
        ("certificates", "证书", "text", []), ("expected_position", "期望职位", "text", []),
        ("expected_location", "期望地点", "singleSelect",
         ["北京", "上海", "深圳", "杭州", "成都", "广州", "南京", "武汉", "曲靖", "不限"]),
        ("expected_salary", "期望薪资", "text", []),
        ("skills", "技能标签", "multipleSelect", ["暖通运维", "PLC编程", "CAD"]),
        ("attachment", "简历附件", "attachment", []), ("upload_time", "上传时间", "date", []),
        ("category", "简历库分类", "singleSelect", ["技术类", "产品类", "市场类", "运营类", "其他"]),
        ("comm_status", "沟通状态", "singleSelect",
         ["已入职", "流程中", "待筛选", "简历未通过", "面试未通过"]),
        ("org", "简历库所属组织", "singleSelect", ["职能中心", "制造中心"]),
        ("full_text", "简历全文", "text", []), ("attach_md5", "附件内容MD5", "text", []),
    ],
    "job": [
        ("job_id", "岗位ID", "text", []), ("job_name", "岗位名称", "text", []),
        ("department", "所属部门", "singleSelect",
         ["技术部", "产品部", "市场部", "运营部", "厂务管理部", "EHS管理部",
          "单晶制造部-生产部", "单晶制造部-设备部", "数据信息部-系统组", "财经管理部",
          "硅片制造部-工艺部", "硅片制造部-设备部", "组件制造部-工艺部", "组件制造部-设备部",
          "电池制造部-工艺部", "电池制造部-设备部"]),
        ("org", "组织分类", "singleSelect", ["职能中心", "制造中心"]),
        ("status", "状态", "singleSelect", ["招聘中", "草稿", "已关闭"]),
        ("work_location", "工作地点", "multipleSelect",
         ["北京", "上海", "深圳", "杭州", "成都", "广州", "南京", "武汉", "曲靖", "不限"]),
        ("responsibilities", "岗位职责", "text", []), ("requirements", "任职要求", "text", []),
        ("hard_gates", "硬性门槛", "text", []), ("must_skills", "必备技能", "text", []),
        ("bonus_skills", "加分项", "text", []), ("must_weight", "必备技能权重", "number", []),
        ("bonus_weight", "加分项权重", "number", []), ("submit_time", "提交时间", "date", []),
        ("attachment", "JD附件", "attachment", []),
        ("stat_total", "候选人总数", "number", []), ("stat_recommend", "推荐数", "number", []),
        ("stat_pending", "待定数", "number", []), ("stat_reject", "不推荐数", "number", []),
    ],
    "match": [
        ("name", "候选人姓名", "text", []), ("phone", "手机号", "telephone", []),
        ("job_name", "匹配岗位", "text", []), ("job_id", "岗位ID", "text", []),
        ("org", "组织分类", "singleSelect", ["职能中心", "制造中心"]),
        ("source", "匹配来源", "singleSelect", ["系统匹配", "人工匹配"]),
        ("cand_skills", "候选人技能", "text", []), ("must_skills", "岗位必备技能", "text", []),
        ("bonus_skills", "岗位加分项", "text", []), ("hard_gates", "硬性门槛", "text", []),
        ("expected_position", "期望职位", "text", []), ("years_experience", "工作年限", "text", []),
        ("skill_score", "技能得分", "number", []), ("bonus_score", "加分项得分", "number", []),
        ("total_score", "匹配总分", "number", []),
        ("recommend", "推荐状态", "singleSelect", ["推荐", "待定", "不推荐"]),
        ("update_time", "更新时间", "date", []), ("evidence", "匹配依据", "text", []),
    ],
    "perm": [
        ("user", "用户", "text", []),
        ("org", "组织分类", "singleSelect", ["职能中心", "制造中心"]),
    ],
}
TABLE_NAMES = {"resume": "简历库管理", "job": "岗位JD表", "match": "智能匹配", "perm": "权限配置"}


def main():
    ap = argparse.ArgumentParser(description="在新 Base 重建四表结构")
    ap.add_argument("base_id")
    ap.add_argument("--operator", default=None, help="覆盖 config 的 operator_id")
    args = ap.parse_args()

    nt = Notable()
    nt.base = args.base_id
    if args.operator:
        nt.op = args.operator
    out = {"base_id": args.base_id, "tables": {}, "fields": {}, "types": {}}
    for key, fdefs in SCHEMA.items():
        r = nt.call("POST", "/v1.0/notable/bases/%s/sheets" % nt.base,
                    {"name": TABLE_NAMES[key], "fields": []})
        sheet = r["id"]
        out["tables"][key] = {"table_id": sheet, "name": TABLE_NAMES[key]}
        out["fields"][key], out["types"][key] = {}, {}
        for _biz, cn, ftype, opts in fdefs:
            body = {"name": cn, "type": ftype}
            if ftype in ("singleSelect", "multipleSelect") and opts:
                body["property"] = {"options": [{"name": o} for o in opts]}
            fr = nt.call("POST", "/v1.0/notable/bases/%s/sheets/%s/fields" % (nt.base, sheet), body)
            out["fields"][key][_biz] = cn
            out["types"][key][_biz] = ftype
            time.sleep(0.2)  # 建字段限流余量
        print("built", TABLE_NAMES[key], sheet, len(fdefs), "fields")
    out["note"] = "replicate_base.py 生成；合并进 config.json 后替换 base_id/tables/fields/types"
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except NotableError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
