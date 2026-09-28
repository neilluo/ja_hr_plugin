# -*- coding: utf-8 -*-
"""check_skill_coverage.py — 岗位技能词表 vs 简历技能标签 的可命中率自检 + 改词机器辅助

用途：必备/加分技能若与简历库标签不同源，匹配分数会恒低或全0。每次 JD 入库、每次简历入库
（或标签池调整）后跑一次，把「可命中」比例低于 50% 的岗位列出来修词。

前置门禁：岗位精析队列非空（真源 shared/refine_loop.py）→ exit 2：三列还是入库正则粗值，
覆盖率结论无意义，先跑 JD 精析流水线（jobs_analyze → sync_job_columns）再来自检。
简历标签池为空（resume 表无 skills，如简历尚未精析）→ 输出 skipped 并 exit 0：
命中率无从谈起，跳过自检而非把全岗误判低覆盖。

池变动告警（实测教训）：岗位链与简历链并行消费队列时，简历精析会把短标签刷成复合标签
（"账务"→"总账处理"），hit() 的子串/分词判定随之翻转——同一份岗位词表在上一个池过线、
在下一个池突然低覆盖，改词结论作废。每次运行与上次快照（OUTDIR/resume_pool_snapshot.json）
比对标签数，变了就打 pool_drift 行提示复跑。快照路径走 OUTDIR，测试重定向即天然隔离
（同 refine 租约教训，禁止漏写真实 outputs/）。

用法:
  python3 skills/job-intake/scripts/check_skill_coverage.py [--min 0.5]
  python3 ... --emit-fixes [OUTDIR]   低覆盖两因分辨，产出两个文件：
      jobs_fix.json         sync_job_columns 兼容 payload（**仅 must_skills**；①类换成建议词、
                             ②类保留原词；不碰 hard_gates/bonus_skills）
      jobs_fix_report.json  逐词诊断 cause/suggested/near_pool/still_low，agent 过目后再落库
  python3 ... --precheck payload.json  写回前预校验：payload 里每岗建议词的命中率，
                             全部 ≥ --min 即 exit 0（可放心 sync），否则 exit 2 并报坏岗
  默认模式 stdout 契约不变（VERDICT 行式逐岗低覆盖 + 汇总行），exit 码语义不变：
  存在低覆盖岗位即 exit 2（含②类合法保留岗——不强行凑 0，按 SKILL.md 汇报说明）。

①/② 分辨用**共享汉字数 ≥2** 的近邻启发，不 import semantic_score.py（match-verify 私有
语义词典，跨 skill 代码 import 违反 AGENTS.md 代码归属条款）。建议词是启发式候选，
必须由 agent 扫 report 复核——同形不同义（如"生产排班"↔"生产计划"）不能盲同义。
"""
import hashlib
import json
import os
import sys
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared"))
from notable import Notable  # noqa: E402
from vocab import toks  # noqa: E402  统一分词（唯一真源 shared/vocab.py，禁止本地再抄 SEP）
import refine_loop  # noqa: E402  队列谓词唯一真源，禁止本地抄副本

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
OUTDIR = os.path.join(ROOT, "outputs")


def hit(cand, need):
    if not cand or not need:
        return False
    if need in cand or cand in need:
        return True
    a, b = set(toks(need)), set(toks(cand))
    return bool(a & b)


def near_tags(w, cands, min_shared=2, top=5):
    """池内与 w 共享 ≥min_shared 个汉字的近邻标签，共享多者先、同分短者先。
    仅服务 ①/② 分辨与建议词生成（启发式），不做语义判断——语义词典是 match-verify 私有。
    min_shared=2 是经验下限：共享 1 字多为"管理/系统/工程"类尾缀噪声（事故管理↔设备管理）。"""
    ws = set(w)
    scored = sorted(((-len(ws & set(c)), len(c), c) for c in cands
                     if c != w and len(ws & set(c)) >= min_shared))
    return [c for _, _, c in scored[:top]]


def pool_drift(cands, outdir):
    """与上次运行的标签池快照比对；数量或集合变了返回 (旧数, 新数)，否则 None，并刷新快照。"""
    path = os.path.join(outdir, "resume_pool_snapshot.json")
    sig = hashlib.sha1("、".join(sorted(cands)).encode("utf-8")).hexdigest()
    old = None
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:  # noqa: BLE001  无快照/坏快照 = 首次运行
        pass
    try:
        os.makedirs(outdir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"count": len(cands), "sig": sig}, f)
    except OSError:
        pass  # 快照尽力而为，不可写不影响自检结论
    if old and (old.get("sig") != sig):
        return old.get("count"), len(cands)
    return None


def evaluate(jobs, cands, min_ratio):
    """逐岗命中率：返回 (low_jobs, empty_tag)。low_jobs 元素 = (fields, must, hits, ratio)。"""
    low, empty_tag = [], 0
    for j in jobs:
        f = j["fields"]
        must = toks(f.get("must_skills"))
        if not must:
            empty_tag += 1
            continue
        hits = [w for w in must if any(hit(c, w) for c in cands)]
        ratio = len(hits) / len(must)
        if ratio < min_ratio:
            low.append((f, must, hits, ratio))
    return low, empty_tag


def emit_fixes(low, cands, min_ratio):
    """两因分辨 → (payload, report)。payload 仅含 must_skills 键，sync_job_columns 直接消费；
    ①类（有 ≥2 字共享近邻）换建议词，②类保留原词，hard_gates/bonus_skills 一律不碰。

    建议词撞车 = ①/② 的确定性仲裁层（实测教训）：near_tags 只看共享汉字数，会把
    应急管理/事故管理/安全评价 三条各自独立的 JD 要求全猜成同一个「安全管理」——
    撞车正说明它只抓到了"管理"类尾缀噪声（同形不同义），不是真同义。故建议词若撞上
    本岗 must 已有的词、或撞上本轮已采纳的替换词，判为噪声：**保留原词、不采纳建议**，
    cause 标 "①词不同源-撞车(疑同形噪声)"，并把 suggested 原样留在报告里供人工翻案。
    历史上此处是按结果去重建词，会把撞车的原词整条删掉（EHS 经理 9→6 词、
    系统工程师 9→7 词），既丢真实职责，又让 post_ratio 的分母被自己缩小、
    still_low 结论虚高。现逐位替换、长度恒等，分母可信。
    """
    payload, report = {}, {}
    for f, must, hits, ratio in low:
        new_must, words = [], []
        collide = 0
        must_set = set(must)
        for w in must:
            if any(hit(c, w) for c in cands):
                cause, near, rep = None, [], w
            else:
                near = near_tags(w, cands)
                if not near:
                    cause, rep = "②库内无此类候选人", w  # 保留原词（有效业务信号）
                elif near[0] in must_set or near[0] in new_must:
                    cause, rep = "①词不同源-撞车(疑同形噪声)", w  # 保留原词，交人工判
                    collide += 1
                else:
                    cause, rep = "①词不同源", near[0]
            words.append({"word": w, "cause": cause,
                          "suggested": near[0] if near and cause else None,
                          "kept": rep, "near": near})
            new_must.append(rep)
        post = [w for w in new_must if any(hit(c, w) for c in cands)]
        post_ratio = len(post) / len(new_must) if new_must else 0.0
        payload[f["job_id"]] = {"must_skills": "、".join(new_must)}
        report[f["job_id"]] = {"job_name": f.get("job_name"), "department": f.get("department"),
                               "pre": "%.0f%%" % (100 * ratio), "post": "%.0f%%" % (100 * post_ratio),
                               "still_low": post_ratio < min_ratio, "collisions": collide,
                               "words": words}
    return payload, report


def precheck(path, jobs, cands, min_ratio):
    """写回前预校验 payload：仅评 payload 里出现的岗、仅评 must_skills 键。
    返回坏岗列表（空 = 可放心 sync）。未知 job_id 单独报（sync 会 unmatched，白跑一次写回）。"""
    payload = json.load(open(path, encoding="utf-8"))
    by_id = {j["fields"].get("job_id"): j["fields"] for j in jobs}
    bad, unknown = [], []
    for key, p in payload.items():
        f = by_id.get(key)
        if f is None:
            unknown.append(key)
            continue
        prop = toks(p.get("must_skills"))
        if not prop:
            continue  # 未提案 must_skills：sync 不改该列，现值是否低覆盖由默认模式管
        hits = [w for w in prop if any(hit(c, w) for c in cands)]
        ratio = len(hits) / len(prop)
        if ratio < min_ratio:
            bad.append({"job_id": key, "job_name": f.get("job_name"),
                        "ratio": "%.0f%%" % (100 * ratio),
                        "miss": [w for w in prop if w not in hits]})
    return bad, unknown


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=0.5, help="必备技能最低可命中比例，默认0.5")
    ap.add_argument("--emit-fixes", nargs="?", const=OUTDIR, metavar="OUTDIR", default=None,
                    help="两因分辨并产出 jobs_fix.json（sync_job_columns payload）+ 逐词诊断报告")
    ap.add_argument("--precheck", metavar="PAYLOAD", default=None,
                    help="写回前预校验 sync payload 的 must_skills 命中率，坏岗 exit 2")
    args = ap.parse_args()

    nt = Notable()
    # 前置门禁（与 match_gated 同纪律）：岗位精析未完成时，三列是入库正则粗值，覆盖率无意义
    if refine_loop.queue(nt, "job"):
        print(json.dumps({"refused": True,
                          "reason": "岗位未精析（队列非空），粗词表覆盖率无意义；先跑 jobs_analyze → sync_job_columns"},
                         ensure_ascii=False))
        sys.exit(2)
    jobs = nt.list_records("job", biz_fields=["job_id", "job_name", "department", "must_skills", "bonus_skills"])
    pool = set()
    for c in nt.list_records("resume", biz_fields=["skills"]):
        pool.update(c["fields"].get("skills") or [])
    cands = sorted(pool)
    if not cands:
        # 简历标签池为空（如简历尚未精析）：命中率无从谈起，跳过自检而非全岗误判低覆盖
        print(json.dumps({"skipped": True, "reason": "简历标签池为空，覆盖率自检无意义",
                          "jobs": len(jobs)}, ensure_ascii=False))
        sys.exit(0)

    drift = pool_drift(cands, OUTDIR)
    if drift:
        print("pool_drift 上次标签池=%s 本次=%d（并发的简历精析刷新过池，上一轮改词结论可能作废，"
              "以本次为准）" % (drift[0], drift[1]))

    # 预校验模式：评 payload 不评现表，通过即可放心 sync（省一次「写回→自检」往返）
    if args.precheck:
        bad, unknown = precheck(args.precheck, jobs, cands, args.min)
        print(json.dumps({"precheck": args.precheck, "ok": not bad and not unknown,
                          "bad": bad, "unknown_job_id": unknown}, ensure_ascii=False))
        sys.exit(0 if not bad and not unknown else 2)

    low, empty_tag = evaluate(jobs, cands, args.min)
    for f, must, hits, ratio in low:
        print("低覆盖 可命中 %d/%d  %-22s %s  未命中词参考: %s"
              % (len(hits), len(must), f.get("job_name"), f.get("job_id"),
                 "、".join([n for n in must if n not in hits][:6])))
    print("岗位数=%d 必备技能空缺=%d 简历标签池=%d 低覆盖=%d" % (len(jobs), empty_tag, len(cands), len(low)))

    if args.emit_fixes is not None:
        if not low:
            print("emit_fixes 无需改词（低覆盖=0），不落 jobs_fix.json")
        else:
            outdir = args.emit_fixes
            payload, report = emit_fixes(low, cands, args.min)
            fp, rp = os.path.join(outdir, "jobs_fix.json"), os.path.join(outdir, "jobs_fix_report.json")
            os.makedirs(outdir, exist_ok=True)
            json.dump(payload, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            json.dump(report, open(rp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            still = [jid for jid, r in report.items() if r["still_low"]]
            print("emit_fixes=%s report=%s 生成=%d岗 改词后仍低(②类保留，汇报说明、勿强行凑0)=%s"
                  % (fp, rp, len(payload), "、".join(still) or "无"))

    sys.exit(2 if low else 0)


if __name__ == "__main__":
    main()
