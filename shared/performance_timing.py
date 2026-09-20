# -*- coding: utf-8 -*-
"""跨进程性能时间线。

run_pipeline、intake 子进程和 build_replay 依次向同一个 JSON 文件追加事件，
用于区分本地处理、宿主编排间隙和 dws 已上报耗时。该文件是诊断旁路产物，
不参与任何业务判断，也不改变 intake_report/dws_results 的既有契约。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from jsonio import write_json_atomic

TIMING_FILE_NAME = "performance_timing.json"
SCHEMA_VERSION = 1


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat()


def _path(out_dir: Any) -> Path:
    return Path(out_dir).expanduser().resolve() / TIMING_FILE_NAME


def _load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _safe_write(path: Path, doc: Dict[str, Any]) -> bool:
    try:
        write_json_atomic(path, doc)
        return True
    except Exception as exc:  # diagnostics must never break business flow
        print("[performance] 计时写入失败（已忽略）: %s: %s"
              % (type(exc).__name__, str(exc)[:200]), file=sys.stderr)
        return False


def timeline_exists(out_dir: Any) -> bool:
    try:
        return _path(out_dir).exists()
    except Exception:
        return False


def _safe_start_run(out_dir, intake_type, source, started_at_ms):
    started = int(started_at_ms if started_at_ms is not None else _now_ms())
    path = _path(out_dir)
    doc = {
        "version": SCHEMA_VERSION,
        "run_id": uuid.uuid4().hex,
        "intake_type": intake_type,
        "source": source,
        "started_at": _iso(started),
        "started_at_ms": started,
        "events": [],
        "dws_observation": {
            "commands_total": 0,
            "results_observed": 0,
            "elapsed_measured": 0,
            "elapsed_missing": 0,
            "elapsed_ms": 0,
        },
        "summary": {},
    }
    _safe_write(path, doc)
    return path


def start_run(out_dir: Any, intake_type: str, source: str,
              started_at_ms: Optional[int] = None) -> Optional[Path]:
    """开始一次新的计时，覆盖同一 out_dir 里的旧诊断数据。"""
    try:
        return _safe_start_run(out_dir, intake_type, source, started_at_ms)
    except Exception as exc:  # 诊断永远不允许冒进业务代码
        _swallow("start_run", exc)
        return None


def _swallow(name: str, exc: BaseException) -> None:
    print("[performance] %s 异常（已忽略）: %s: %s"
          % (name, type(exc).__name__, str(exc)[:200]), file=sys.stderr)


def append_event(out_dir: Any, name: str, category: str,
                 started_at_ms: int, finished_at_ms: int,
                 dws_calls: int = 0,
                 metadata: Optional[Dict[str, Any]] = None) -> Path:
    """追加一个事件并刷新汇总。"""
    return append_events(out_dir, [{
        "name": name,
        "category": category,
        "started_at_ms": started_at_ms,
        "finished_at_ms": finished_at_ms,
        "dws_calls": dws_calls,
        "metadata": metadata,
    }])


def _append_to_doc(doc: Dict[str, Any], events: Iterable[Dict[str, Any]]) -> None:
    target = doc.setdefault("events", [])
    for item in events:
        started_at_ms = int(item["started_at_ms"])
        finished_at_ms = int(item["finished_at_ms"])
        event = {
            "name": item["name"],
            "category": item["category"],
            "started_at": _iso(started_at_ms),
            "finished_at": _iso(finished_at_ms),
            "started_at_ms": started_at_ms,
            "finished_at_ms": finished_at_ms,
            "elapsed_ms": max(0, int(item.get("elapsed_ms")))
            if item.get("elapsed_ms") is not None
            else max(0, finished_at_ms - started_at_ms),
            "dws_calls": max(0, int(item.get("dws_calls") or 0)),
        }
        if item.get("metadata"):
            event["metadata"] = item["metadata"]
        target.append(event)


def append_events(out_dir: Any, events: Iterable[Dict[str, Any]]) -> Optional[Path]:
    """批量追加事件，仅执行一次原子写，避免埋点自身放大文件 I/O。"""
    try:
        path = _path(out_dir)
        doc = _load(path)
        events_in = list(events)
        if not doc:
            first_started = (int(events_in[0].get("started_at_ms"))
                             if events_in else _now_ms())
            _safe_start_run(out_dir, "unknown", "standalone", first_started)
            doc = _load(path)
        _append_to_doc(doc, events_in)
        _refresh_summary(doc)
        _safe_write(path, doc)
        return path
    except Exception as exc:
        _swallow("append_events", exc)
        return None



def _collect_dws_observation(out_dir: Any, commands: Iterable[Dict[str, Any]],
                             read_result: Any, scope_complete: bool,
                             scope: str) -> Dict[str, Any]:
    root = Path(out_dir)
    total = observed = measured = missing = elapsed = 0
    by_type: Dict[str, Dict[str, int]] = {}
    for cmd in commands:
        total += 1
        seq = cmd.get("seq")
        result_name = cmd.get("result_file") or ("dws_out_%s.json" % seq)
        result = read_result(root / str(result_name))
        if result is None:
            continue
        observed += 1
        cmd_type = str(cmd.get("command_type") or "unknown")
        bucket = by_type.setdefault(cmd_type, {"count": 0, "measured": 0,
                                               "elapsed_ms": 0})
        bucket["count"] += 1
        if result.get("elapsed_measured"):
            value = max(0, int(result.get("elapsed_ms") or 0))
            measured += 1
            elapsed += value
            bucket["measured"] += 1
            bucket["elapsed_ms"] += value
        else:
            missing += 1
    return {
        "scope": scope,
        "scope_complete": bool(scope_complete),
        "commands_total": total,
        "results_observed": observed,
        "elapsed_measured": measured,
        "elapsed_missing": missing,
        "elapsed_ms": elapsed,
        "coverage_ratio": round(measured / float(observed), 4) if observed else 0.0,
        "by_type": by_type,
    }


def _observation_rank(obs: Dict[str, Any]) -> tuple:
    """数值越高代表越可信：先看是否全量覆盖，再看已观测/已测量的数量。"""
    return (1 if obs.get("scope_complete") else 0,
            int(obs.get("results_observed") or 0),
            int(obs.get("elapsed_measured") or 0))


def _merge_observation(current: Optional[Dict[str, Any]],
                       new: Dict[str, Any]) -> Dict[str, Any]:
    """只在新的观测至少同等可信时才覆盖，避免部分观测把全量观测缩水。"""
    if not current:
        return new
    return new if _observation_rank(new) >= _observation_rank(current) else current


def observe_dws_results(out_dir: Any, commands: Iterable[Dict[str, Any]],
                        read_result: Any, *, scope_complete: bool = True,
                        scope: str = "emit_commands") -> Dict[str, Any]:
    """扫描 dws 输出，写入耗时观测；部分观测不会覆盖已存在的全量观测。"""
    try:
        observation = _collect_dws_observation(out_dir, commands, read_result,
                                               scope_complete, scope)
        path = _path(out_dir)
        doc = _load(path)
        if not doc:
            _safe_start_run(out_dir, "unknown", "build_replay", _now_ms())
            doc = _load(path)
        doc["dws_observation"] = _merge_observation(doc.get("dws_observation"),
                                                    observation)
        _refresh_summary(doc)
        _safe_write(path, doc)
        return doc["dws_observation"]
    except Exception as exc:
        _swallow("observe_dws_results", exc)
        return {}


def append_event_and_observe(out_dir: Any, event: Dict[str, Any],
                             commands: Iterable[Dict[str, Any]], read_result: Any,
                             *, scope_complete: bool = True,
                             scope: str = "emit_commands") -> Dict[str, Any]:
    """一次原子写同时记录本地阶段与 dws 观测，供高频 build/附件路径使用。"""
    try:
        path = _path(out_dir)
        doc = _load(path)
        if not doc:
            _safe_start_run(out_dir, "unknown", "standalone",
                            int(event["started_at_ms"]))
            doc = _load(path)
        _append_to_doc(doc, [event])
        observation = _collect_dws_observation(out_dir, commands, read_result,
                                               scope_complete, scope)
        doc["dws_observation"] = _merge_observation(doc.get("dws_observation"),
                                                    observation)
        _refresh_summary(doc)
        _safe_write(path, doc)
        return doc["dws_observation"]
    except Exception as exc:
        _swallow("append_event_and_observe", exc)
        return {}


def read_summary(out_dir: Any) -> Dict[str, Any]:
    doc = _load(_path(out_dir))
    return dict(doc.get("summary") or {})


def _refresh_summary(doc: Dict[str, Any]) -> None:
    events = list(doc.get("events") or [])
    top = [e for e in events if e.get("category") == "orchestrator_local"]
    local_ms = sum(max(0, int(e.get("elapsed_ms") or 0)) for e in top)
    started = int(doc.get("started_at_ms") or (events[0].get("started_at_ms") if events else _now_ms()))
    finished = max([started] + [int(e.get("finished_at_ms") or started) for e in events])
    wall_ms = max(0, finished - started)
    gap_ms = max(0, wall_ms - local_ms)
    dws = dict(doc.get("dws_observation") or {})
    observed = int(dws.get("results_observed") or 0)
    measured = int(dws.get("elapsed_measured") or 0)
    dws_ms = int(dws.get("elapsed_ms") or 0)
    total_commands = int(dws.get("commands_total") or 0)
    coverage_complete = bool(dws.get("scope_complete") and total_commands
                             and observed == total_commands and measured == observed)
    slowest_stages = sorted(
        ({"name": e.get("name"), "elapsed_ms": int(e.get("elapsed_ms") or 0),
          "dws_calls": int(e.get("dws_calls") or 0)}
         for e in events if e.get("category") == "intake_stage"),
        key=lambda item: item["elapsed_ms"], reverse=True)[:5]
    stage_total_ms = sum(int(e.get("elapsed_ms") or 0)
                         for e in events if e.get("category") == "intake_stage")
    local_unattributed_ms = max(0, local_ms - stage_total_ms)
    ordered_top = sorted(top, key=lambda e: int(e.get("started_at_ms") or 0))
    gaps = []
    previous = None
    for event in ordered_top:
        if previous is not None:
            gap = max(0, int(event.get("started_at_ms") or 0)
                      - int(previous.get("finished_at_ms") or 0))
            if gap:
                gaps.append({"after": previous.get("name"), "before": event.get("name"),
                             "elapsed_ms": gap})
        previous = event
    doc["updated_at"] = _iso(_now_ms())
    doc["summary"] = {
        "pipeline_wall_ms": wall_ms,
        "local_orchestrator_ms": local_ms,
        "orchestration_gap_ms": gap_ms,
        "dws_reported_elapsed_ms": dws_ms,
        "dws_elapsed_coverage": round(measured / float(observed), 4) if observed else 0.0,
        "dws_share_of_wall": (round(dws_ms / float(wall_ms), 4)
                              if wall_ms and coverage_complete else None),
        "orchestration_gap_share_of_wall": (round(gap_ms / float(wall_ms), 4)
                                             if wall_ms else 0.0),
        "intake_stage_total_ms": stage_total_ms,
        "local_unattributed_ms": local_unattributed_ms,
        "slowest_intake_stages": slowest_stages,
        "orchestration_gaps": gaps,
        "note": ("dws_share_of_wall 仅在全部已观测结果包含 elapsed_ms 时计算；"
                 "orchestration_gap 包含 dws、agent 调度及人工停顿，只是宿主往返上界。"),
    }


def child_timing_enabled() -> bool:
    """run_pipeline 启动的 intake 子进程会继承此标记，避免重置时间线。"""
    return os.environ.get("RECRUIT_TIMING_PARENT") == "1"
