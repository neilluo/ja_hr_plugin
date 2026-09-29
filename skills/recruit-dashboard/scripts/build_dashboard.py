#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_dashboard.py — 从岗位表+匹配表只读生成单文件 HTML 招聘看板。

用法（以仓库根为 CWD）:
    python3 skills/recruit-dashboard/scripts/build_dashboard.py                 # 默认写 outputs/dashboard.html
    python3 skills/recruit-dashboard/scripts/build_dashboard.py out.html --top 10

确定性聚合一律在本脚本内完成（漏斗合计 / Top-N 排序 / 部门分布 / 数据截止时间），
agent 只负责运行并转述 stdout 的一行摘要，禁止手拼 HTML 或手算数字。

只读纪律：仅调用 Notable.list_records，绝不写表。
字段中文名/推荐标签/日期显示格式一律取自 config.json（唯一真源），本脚本不抄清单。
空表优雅降级：job=0 渲染空态看板，不崩溃。
"""

import argparse
import html
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "shared", "preflight"))
from notable import Notable  # noqa: E402
from preflight import run_preflight  # noqa: E402

_CONFIG = os.path.join(ROOT, "config.json")

# 岗位业务键（读取投影）：清单以 config.fields.job 为准，此处只是取数用到的子集声明
_JOB_FIELDS = ["job_id", "job_name", "department", "status", "submit_time",
               "stat_total", "stat_recommend", "stat_pending", "stat_reject"]
_MATCH_FIELDS = ["name", "job_name", "total_score", "recommend", "evidence"]

# 写入/读取值字面量例外（AGENTS 不变量 10）：在招状态标签单一出现，真源 config.options.job.status
_STATUS_OPEN = "招聘中"


def load_config(path=None):
    with open(path or _CONFIG, encoding="utf-8") as f:
        return json.load(f)


def _num(v):
    """stat_*/total_score 读回可能是 float/str/None，统一成可算数的 float 或 None。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_ms(ms, cfg, tkey="job", biz="submit_time"):
    """毫秒时间戳 → 显示串。格式派生自 config.formats.date（唯一真源），不抄字面量。
    config 未声明该键时（正常不会发生：job.submit_time 已声明）退化为本地时间
    年-月-日 时:分 的 strftime 计算，仅作显示兜底，不构成 formatter 第二真源。"""
    pattern = ((cfg.get("formats", {}).get("date") or {}).get(tkey, {}) or {}).get(biz)
    try:
        if pattern:
            py = (pattern.replace("YYYY", "%Y").replace("MM", "%m").replace("DD", "%d")
                  .replace("HH", "%H").replace("mm", "%M").replace("ss", "%S"))
        else:
            py = "%Y-%m-%d %H:%M"   # 计算兜底：非 config formatter 副本（config 声明路径为主）
        return time.strftime(py, time.localtime(float(ms) / 1000.0))
    except (TypeError, ValueError, OSError):
        return "—"


def fetch(nt, cfg):
    """只读拉取 job/match 两表投影字段。返回 (jobs, matches) 行列表。"""
    jobs = nt.list_records("job", biz_fields=_JOB_FIELDS)
    matches = nt.list_records("match", biz_fields=_MATCH_FIELDS)
    return jobs, matches


def aggregate(jobs, matches, cfg, top=20):
    """确定性聚合：漏斗合计 / Top-N 推荐榜 / 部门分布 / as_of。纯函数，测试直喂假数据。"""
    rec_label = cfg["options"]["match"]["recommend"][0]   # 推荐标签派生自 config（不变量 10）
    rec_labels = list(cfg["options"]["match"]["recommend"])

    funnel = {"total": 0.0, "recommend": 0.0, "pending": 0.0, "reject": 0.0}
    unmatched = 0
    open_by_dept, all_by_dept = {}, {}
    submit_times = []
    job_rows = []
    for r in jobs:
        f = r.get("fields", {})
        st = _num(f.get("stat_total"))
        if st is None:
            unmatched += 1
        else:
            funnel["total"] += st
            funnel["recommend"] += _num(f.get("stat_recommend")) or 0.0
            funnel["pending"] += _num(f.get("stat_pending")) or 0.0
            funnel["reject"] += _num(f.get("stat_reject")) or 0.0
        dept = f.get("department") or "未填部门"
        all_by_dept[dept] = all_by_dept.get(dept, 0) + 1
        if f.get("status") == _STATUS_OPEN:
            open_by_dept[dept] = open_by_dept.get(dept, 0) + 1
        if f.get("submit_time"):
            submit_times.append(f["submit_time"])
        job_rows.append({"job_name": f.get("job_name") or "?", "department": dept,
                         "status": f.get("status") or "", "stat_total": st})

    cands = [r.get("fields", {}) for r in matches]
    recs = [c for c in cands if c.get("recommend") == rec_label]
    recs.sort(key=lambda c: _num(c.get("total_score")) or 0.0, reverse=True)
    top_cands = [{"name": c.get("name") or "?", "job_name": c.get("job_name") or "?",
                  "total_score": _num(c.get("total_score")),
                  "evidence": c.get("evidence") or ""} for c in recs[:top]]

    as_of_ms = max(submit_times) if submit_times else None
    departments = sorted(((d, open_by_dept.get(d, 0), n) for d, n in all_by_dept.items()),
                         key=lambda x: (-x[2], x[0]))
    return {
        "job_count": len(jobs),
        "match_count": len(matches),
        "funnel": funnel,
        "unmatched_jobs": unmatched,
        "top_candidates": top_cands,
        "departments": departments,      # [(部门, 在招数, 岗位总数)] 按总数降序
        "as_of_ms": as_of_ms,
        "as_of": fmt_ms(as_of_ms, cfg) if as_of_ms else "—",
        "rec_label": rec_label,
        "rec_labels": rec_labels,
        "job_rows": job_rows,
    }


_CSS = """
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;
     background:#f5f6f8;color:#222;}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px 48px;}
h1{font-size:22px;margin:0 0 4px;} h2{font-size:16px;margin:0 0 12px;}
.asof{color:#666;font-size:13px;margin-bottom:20px;}
.card{background:#fff;border:1px solid #e3e5e8;border-radius:8px;padding:16px;margin-bottom:16px;}
.bar-row{display:flex;align-items:center;margin:6px 0;font-size:13px;}
.bar-label{width:88px;flex:none;color:#444;}
.bar-track{flex:1;background:#eef0f3;border-radius:4px;height:18px;overflow:hidden;}
.bar-fill{height:100%;border-radius:4px;}
.bar-num{width:72px;flex:none;text-align:right;color:#333;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eef0f3;}
th{color:#666;font-weight:500;}
.empty{color:#888;font-size:13px;padding:8px 0;}
"""

_BAR_COLORS = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444"]


def _bar(label, value, maxv, color):
    pct = int(round(100.0 * value / maxv)) if maxv else 0
    num = ("%g" % value)
    return ('<div class="bar-row"><span class="bar-label">%s</span>'
            '<div class="bar-track"><div class="bar-fill" style="width:%d%%;background:%s"></div></div>'
            '<span class="bar-num">%s</span></div>'
            % (html.escape(str(label)), pct, color, html.escape(num)))


def render_html(agg, cfg, top_n):
    """聚合结果 → 单文件 HTML（内联 CSS、无外部依赖、扁平风、中文标签）。纯函数。"""
    e = html.escape
    parts = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>招聘看板</title><style>%s</style></head><body><div class="wrap">' % _CSS,
             '<h1>招聘看板</h1>',
             '<div class="asof">数据截止时间：%s ｜ 岗位 %d 个 ｜ 匹配记录 %d 条</div>'
             % (e(agg["as_of"]), agg["job_count"], agg["match_count"])]

    if agg["job_count"] == 0:
        parts.append('<div class="card"><div class="empty">岗位表为空：先用 job-intake 入库岗位 JD，'
                     '再跑 match-verify 生成匹配，最后重跑本看板。</div></div>')
        parts.append('</div></body></html>')
        return "".join(parts)

    # ① 岗位漏斗
    f = agg["funnel"]
    rl = list(agg["rec_labels"])          # [推荐, 待定, 不推荐]，顺序派生自 config.options
    labels = ["候选人总数"] + rl
    vals = [f["total"], f["recommend"], f["pending"], f["reject"]]
    maxv = max(vals) if vals else 0
    parts.append('<div class="card"><h2>岗位漏斗（全部岗位合计）</h2>')
    if agg["match_count"] == 0:
        parts.append('<div class="empty">匹配表为空：先跑 match-verify 生成匹配记录。</div>')
    for i, (lab, v) in enumerate(zip(labels, vals)):
        parts.append(_bar(lab, v, maxv, _BAR_COLORS[i % len(_BAR_COLORS)]))
    if agg["unmatched_jobs"]:
        parts.append('<div class="empty">%d 个岗位尚未跑匹配（stat_* 为空），未计入漏斗。</div>'
                     % agg["unmatched_jobs"])
    parts.append('</div>')

    # ② 推荐 Top 榜
    parts.append('<div class="card"><h2>%s Top %d（按匹配总分降序）</h2>' % (e(agg["rec_label"]), top_n))
    if agg["top_candidates"]:
        parts.append('<table><tr><th>#</th><th>候选人</th><th>匹配岗位</th>'
                     '<th>总分</th><th>匹配依据</th></tr>')
        for i, c in enumerate(agg["top_candidates"], 1):
            score = "%g" % c["total_score"] if c["total_score"] is not None else "—"
            parts.append('<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                         % (i, e(c["name"]), e(c["job_name"]), e(score), e(c["evidence"])))
        parts.append('</table>')
    else:
        parts.append('<div class="empty">暂无「%s」记录。</div>' % e(agg["rec_label"]))
    parts.append('</div>')

    # ③ 部门分布
    parts.append('<div class="card"><h2>部门分布（岗位数）</h2>')
    dmax = max((n for _, _, n in agg["departments"]), default=0)
    for dept, n_open, n_all in agg["departments"]:
        parts.append(_bar("%s（在招%d）" % (dept, n_open), n_all, dmax, "#6366f1"))
    parts.append('</div>')

    # 附：岗位清单
    parts.append('<div class="card"><h2>岗位清单</h2><table>'
                 '<tr><th>岗位</th><th>部门</th><th>状态</th><th>候选人总数</th></tr>')
    for j in agg["job_rows"]:
        st = "%g" % j["stat_total"] if j["stat_total"] is not None else "未匹配"
        parts.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                     % (e(j["job_name"]), e(j["department"]), e(j["status"]), e(st)))
    parts.append('</table></div>')

    parts.append('</div></body></html>')
    return "".join(parts)


def build(nt, cfg, out_path, top):
    """fetch → aggregate → 写 HTML → 返回一行摘要（agent 原样转述）。"""
    jobs, matches = fetch(nt, cfg)
    agg = aggregate(jobs, matches, cfg, top=top)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(agg, cfg, top))
    return ("看板已生成：%d 岗位 / %d 匹配记录 / 数据截止 %s → %s"
            % (agg["job_count"], agg["match_count"], agg["as_of"], out_path))


def main(argv=None):
    ap = argparse.ArgumentParser(description="只读生成单文件 HTML 招聘看板")
    ap.add_argument("output", nargs="?", default=os.path.join("outputs", "dashboard.html"),
                    help="输出 HTML 路径，默认 outputs/dashboard.html")
    ap.add_argument("--top", type=int, default=20, help="推荐榜条数，默认 20")
    args = ap.parse_args(argv)

    # stage 0: 环境预检（凭证/依赖），与其他入口一致
    run_preflight(config_path=_CONFIG)
    cfg = load_config()
    nt = Notable()
    print(build(nt, cfg, args.output, args.top))


if __name__ == "__main__":
    main()
