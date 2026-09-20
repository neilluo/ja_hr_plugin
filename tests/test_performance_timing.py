# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT / "shared", ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import performance_timing
from build_replay import read_dws_output
from performance_timing import (
    append_event,
    append_events,
    observe_dws_results,
    read_summary,
    start_run,
)
from upload_attachments import unwrap_dws_json


class PerformanceTimingTest(unittest.TestCase):
    def test_summary_separates_local_time_from_orchestration_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            start_run(tmp, "resume", "test", 1000)
            append_events(tmp, [
                {"name": "resume.extract", "category": "intake_stage",
                 "started_at_ms": 1000, "finished_at_ms": 1070},
                {"name": "run_pipeline.emit", "category": "orchestrator_local",
                 "started_at_ms": 1000, "finished_at_ms": 1100},
            ])
            append_event(tmp, "build_replay", "orchestrator_local", 1300, 1350)

            summary = read_summary(tmp)
            self.assertEqual(summary["pipeline_wall_ms"], 350)
            self.assertEqual(summary["local_orchestrator_ms"], 150)
            self.assertEqual(summary["orchestration_gap_ms"], 200)

    def test_dws_share_requires_complete_elapsed_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            start_run(tmp, "resume", "test", 1000)
            append_event(tmp, "run_pipeline.emit", "orchestrator_local", 1000, 1100)
            append_event(tmp, "build_replay", "orchestrator_local", 1300, 1400)
            commands = [
                {"seq": 1, "command_type": "record query"},
                {"seq": 2, "command_type": "record upsert"},
            ]
            (root / "dws_out_1.json").write_text(json.dumps({
                "returncode": 0, "stdout": "{}", "stderr": "", "elapsed_ms": 80,
            }), encoding="utf-8")

            observed = observe_dws_results(tmp, commands, read_dws_output)
            self.assertEqual(observed["results_observed"], 1)
            self.assertEqual(observed["elapsed_measured"], 1)
            self.assertIsNone(read_summary(tmp)["dws_share_of_wall"])

            (root / "dws_out_2.json").write_text("{}", encoding="utf-8")
            observed = observe_dws_results(tmp, commands, read_dws_output)
            self.assertEqual(observed["elapsed_measured"], 1)
            self.assertEqual(observed["elapsed_missing"], 1)
            self.assertIsNone(read_summary(tmp)["dws_share_of_wall"])

            (root / "dws_out_2.json").write_text(json.dumps({
                "returncode": 0, "stdout": "{}", "stderr": "", "elapsed_ms": 20,
            }), encoding="utf-8")
            observed = observe_dws_results(tmp, commands, read_dws_output)
            self.assertEqual(observed["elapsed_ms"], 100)
            self.assertEqual(read_summary(tmp)["dws_share_of_wall"], 0.25)

    def test_legacy_dws_output_is_not_reported_as_measured_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dws_out_1.json"
            path.write_text('{"status":"success","data":{}}', encoding="utf-8")
            result = read_dws_output(path)
            self.assertEqual(result["elapsed_ms"], 0)
            self.assertFalse(result["elapsed_measured"])

    def test_invalid_elapsed_values_are_not_measured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dws_out_1.json"
            for value in (None, True, -1, "12"):
                path.write_text(json.dumps({"stdout": "{}", "elapsed_ms": value}),
                                encoding="utf-8")
                result = read_dws_output(path)
                self.assertFalse(result["elapsed_measured"])
                self.assertEqual(result["elapsed_ms"], 0)

    def test_timing_write_failure_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(performance_timing, "write_json_atomic",
                                   side_effect=PermissionError("blocked")):
                path = start_run(tmp, "resume", "test", 1000)
                self.assertEqual(path.name, "performance_timing.json")
                append_event(tmp, "emit", "orchestrator_local", 1000, 1100)

    def test_partial_observation_does_not_overwrite_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            start_run(tmp, "resume", "test", 1000)
            (root / "dws_out_1.json").write_text(json.dumps({
                "returncode": 0, "stdout": "{}", "stderr": "", "elapsed_ms": 50,
            }), encoding="utf-8")
            (root / "dws_out_update.json").write_text(json.dumps({
                "elapsed_ms": 20,
            }), encoding="utf-8")
            # 先写入一次全量观测
            observe_dws_results(tmp, [{"seq": 1, "command_type": "record query"}],
                                read_dws_output, scope_complete=True)
            # 再触发一次部分观测
            observe_dws_results(tmp,
                                [{"seq": "update",
                                  "result_file": "dws_out_update.json",
                                  "command_type": "record update"}],
                                lambda p: {"elapsed_ms": 20,
                                           "elapsed_measured": True} if p.exists() else None,
                                scope_complete=False,
                                scope="attachment_workflow_partial")
            stored = json.loads((root / "performance_timing.json").read_text())
            self.assertTrue(stored["dws_observation"]["scope_complete"])
            self.assertEqual(stored["dws_observation"]["elapsed_ms"], 50)

    def test_calculation_error_is_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 事件缺必填字段 → 应被兜底吞掉返回 None，不抛给业务
            self.assertIsNone(append_events(tmp, [{"broken": "shape"}]))
            # read_result 恒为 None → 观测结果为空但合法，不抛异常
            result = observe_dws_results(tmp, [{"seq": 9}], lambda _p: None)
            self.assertIsInstance(result, dict)
            self.assertEqual(result.get("results_observed"), 0)

    def test_attachment_wrapper_keeps_business_payload(self) -> None:
        payload = {"status": "success", "data": {
            "fileToken": "ft_test", "uploadUrl": "https://example.invalid/upload"}}
        wrapped = {"returncode": 0, "stdout": json.dumps(payload),
                   "stderr": "", "elapsed_ms": 42}
        self.assertEqual(unwrap_dws_json(wrapped), payload)


if __name__ == "__main__":
    unittest.main()
