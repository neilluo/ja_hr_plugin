# -*- coding: utf-8 -*-
"""ExtractionRunner：并发提取池编排（P7 刀3）。

收拢 skills/resume-intake/scripts/intake_resume.py 阶段1 的线程池：
  建池（workers = max(1, min(concurrency, len(files)))）
  index-keyed 结果收集（ex_by_idx[i] = fut.result()，**按输入下标存**）
  预算触顶 cancel（遍历 futs 取消未开始的，标记 budget.budget_stopped）
  提取摘要交给 Console（打印用 workers 表达式**刻意不与建池合并**）

红线（刀2 遗留提示④，逐字保持）：
  * 两处 workers 表达式（建池 `max(1, min(C, n))` vs 打印 `min(C, max(1, n))`）
    数学等价但**不得合并**——保持两个表达式原样，防「只改建池侧」的静默失真
    （oracle 不校验「并发 %d」这个数字，合并后改一侧不会有任何断言报错）。
  * index-keyed 收集顺序一变则 seq 漂移污染下游 digest（candidates key
    "c%02d" % seq），收集逻辑逐字搬。
  * 提取失败/扫描件进入 Vision 梯队的判定与顺序不变；20% vision gate 的
    判定本体留在 run()，本类只搬池与收集。

npages carry-forward：extract_text 返回的 dict（含 npages，chain 在终态/赢家
里保留首个非零值）原样进 ex_by_idx，本类不修改任何提取结果字段。
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

from extract_text import extract_text


def _safe_extract_text(fp: str) -> Dict[str, Any]:
    """线程池里的提取入口。extract_text 契约上永不抛异常，这里再兜一层防御。"""
    try:
        return extract_text(str(Path(fp).expanduser()))
    except Exception as exc:                        # pragma: no cover
        return {"status": "error", "text": "", "md5": "", "size": 0,
                "kind": None, "backend": "none", "elapsed_ms": 0, "ext": None,
                "error": "提取线程异常 %s: %s" % (type(exc).__name__, exc)}


class ExtractionRunner:
    def __init__(self, concurrency: int, console: Any) -> None:
        self.concurrency = concurrency
        self.console = console

    def run(self, files: List[str], budget: Any) -> Dict[int, Dict[str, Any]]:
        """建池 + index-keyed 收集 + 预算触顶 cancel。返回 ex_by_idx。

        被 cancel 的 future 在 ex_by_idx 里**没有条目**——「没结果 = None =
        未完成（pending_budget）」是隐式契约，entries 骨架组装侧据此判定。
        """
        ex_by_idx: Dict[int, Dict[str, Any]] = {}
        if files:
            workers = max(1, min(self.concurrency, len(files)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(_safe_extract_text, fp): i
                        for i, fp in enumerate(files)}
                for fut in as_completed(futs):
                    i = futs[fut]
                    try:
                        ex_by_idx[i] = fut.result()
                    except Exception as exc:            # pragma: no cover（防御）
                        ex_by_idx[i] = {
                            "status": "error", "text": "", "md5": "", "size": 0,
                            "kind": None, "backend": "none", "elapsed_ms": 0,
                            "ext": None,
                            "error": "提取线程异常 %s: %s" % (type(exc).__name__, exc)}
                    if budget.over_budget():
                        for f2 in futs:
                            f2.cancel()                 # 只取消还没开始的；在跑的会跑完
                        budget.stop_extraction()
                        break
        return ex_by_idx

    def summarize(self, entries: List[Dict[str, Any]], files: List[str],
                  n_ok: int, n_pending: int, n_vision: int,
                  extract_ms: int) -> None:
        """提取摘要交给 Console。打印用 workers 表达式刻意不与建池合并（红线）。"""
        self.console.extract_summary(
            len(entries),
            min(self.concurrency, max(1, len(files))),
            n_ok, n_pending, n_vision, extract_ms)
