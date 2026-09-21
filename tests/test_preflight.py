#!/usr/bin/env python3
"""preflight 本地无副作用单测：不触网，覆盖五项检查的通过与阻断路径。

风格对齐 tests/test_notable_local.py：unittest + tempfile 造文件系统夹具，
环境变量用 unittest.mock.patch.dict 打补丁。凭证统一走 secrets_path/env 夹具，
不依赖仓库根的真实 .secrets.json。
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))

from preflight import run_preflight  # noqa: E402

# 所有用例默认清空钉钉环境变量，凭证由夹具显式提供，避免受宿主环境干扰
_NO_ENV = {"DINGTALK_APP_KEY": "", "DINGTALK_APP_SECRET": ""}


def _run(**kwargs):
    """跑 run_preflight，屏蔽 stdout/stderr，返回 (SystemExit码或None, stdout, checks)。"""
    out, err = io.StringIO(), io.StringIO()
    code = None
    checks = None
    try:
        with redirect_stdout(out), redirect_stderr(err):
            _, checks = run_preflight(**kwargs)
    except SystemExit as e:  # 阻断路径
        code = e.code
    return code, out.getvalue(), checks


def _preflight_json(stdout_text):
    """从 stdout 抽出 PREFLIGHT:{...} 行并解析。"""
    for line in stdout_text.splitlines():
        if line.startswith("PREFLIGHT:"):
            return json.loads(line[len("PREFLIGHT:"):])
    return None


class _PreflightCase(unittest.TestCase):
    """公共夹具：临时目录里造 config.json 与 .secrets.json。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"base_id": "BASE123", "tables": {"resume": {"table_id": "S1"}}}, f)

        self.secrets = os.path.join(self.tmp, ".secrets.json")
        with open(self.secrets, "w", encoding="utf-8") as f:
            json.dump({"app_key": "k", "app_secret": "s"}, f)

    def basic_kwargs(self, **over):
        kw = {"config_path": self.config, "secrets_path": self.secrets}
        kw.update(over)
        return kw


class TestPythonCheck(unittest.TestCase):
    def test_passes_on_running_interpreter(self):
        # 能跑到这里说明解释器可用；env 凭证齐备 + 全部跳过 → python 关必过
        env = {"DINGTALK_APP_KEY": "k", "DINGTALK_APP_SECRET": "s"}
        with patch.dict(os.environ, env, clear=False):
            with redirect_stderr(io.StringIO()):
                ok, checks = run_preflight()
        self.assertTrue(ok)
        self.assertTrue(checks["python"]["pass"])
        self.assertEqual(checks["python"]["info"],
                         "%d.%d.%d" % sys.version_info[:3])


class TestCredentials(_PreflightCase):
    def test_missing_credentials_blocks(self):
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, out, _ = _run(**self.basic_kwargs(secrets_path=os.path.join(self.tmp, "nope.json")))
        self.assertEqual(code, 1)
        body = _preflight_json(out)
        self.assertIsNotNone(body, "必须输出 PREFLIGHT:{...} JSON 行")
        self.assertEqual(body["blocker"], "credentials")
        self.assertIn("凭证缺失", body["next_action"])

    def test_env_vars_pass(self):
        env = {"DINGTALK_APP_KEY": "k", "DINGTALK_APP_SECRET": "s"}
        with patch.dict(os.environ, env, clear=False):
            # secrets_path 指向不存在的文件也应通过（env 优先）
            code, _out, checks = _run(
                **self.basic_kwargs(secrets_path=os.path.join(self.tmp, "nope.json")))
        self.assertIsNone(code)
        self.assertTrue(checks["credentials"]["pass"])

    def test_malformed_secrets_blocks(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, out, _ = _run(**self.basic_kwargs(secrets_path=bad))
        self.assertEqual(code, 1)
        self.assertEqual(_preflight_json(out)["blocker"], "credentials")

    def test_camelcase_keys_accepted(self):
        alt = os.path.join(self.tmp, "camel.json")
        with open(alt, "w", encoding="utf-8") as f:
            json.dump({"appKey": "k", "appSecret": "s"}, f)
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, _out, checks = _run(**self.basic_kwargs(secrets_path=alt))
        self.assertIsNone(code)
        self.assertTrue(checks["credentials"]["pass"])


class TestConfig(_PreflightCase):
    def test_missing_config_blocks(self):
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, out, _ = _run(**self.basic_kwargs(config_path=os.path.join(self.tmp, "gone.json")))
        self.assertEqual(code, 1)
        body = _preflight_json(out)
        self.assertEqual(body["ok"], False)
        self.assertEqual(body["blocker"], "config")

    def test_malformed_config_missing_base_id(self):
        bad = os.path.join(self.tmp, "bad-config.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"tables": {"resume": {}}}, f)  # 无 base_id
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, out, _ = _run(**self.basic_kwargs(config_path=bad))
        self.assertEqual(code, 1)
        self.assertEqual(_preflight_json(out)["blocker"], "config")

    def test_priority_credentials_before_config(self):
        # 凭证与 config 同时坏 → blocker 应为 credentials（优先级更高）
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, out, _ = _run(
                config_path=os.path.join(self.tmp, "gone.json"),
                secrets_path=os.path.join(self.tmp, "nope.json"))
        self.assertEqual(code, 1)
        self.assertEqual(_preflight_json(out)["blocker"], "credentials")


class TestFilesDir(_PreflightCase):
    def test_nonexistent_dir_blocks(self):
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, out, _ = _run(**self.basic_kwargs(files_dir=os.path.join(self.tmp, "nodir")))
        self.assertEqual(code, 1)
        self.assertEqual(_preflight_json(out)["blocker"], "files_dir")

    def test_empty_dir_blocks(self):
        d = os.path.join(self.tmp, "empty")
        os.makedirs(d)
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, out, _ = _run(**self.basic_kwargs(files_dir=d))
        self.assertEqual(code, 1)
        self.assertEqual(_preflight_json(out)["blocker"], "files_dir")

    def test_no_supported_ext_blocks(self):
        d = os.path.join(self.tmp, "txtonly")
        os.makedirs(d)
        with open(os.path.join(d, "readme.txt"), "w") as f:
            f.write("x")
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, out, _ = _run(**self.basic_kwargs(files_dir=d))
        self.assertEqual(code, 1)
        self.assertEqual(_preflight_json(out)["blocker"], "files_dir")

    def test_supported_ext_passes(self):
        d = os.path.join(self.tmp, "resumes")
        os.makedirs(d)
        with open(os.path.join(d, "a.pdf"), "wb") as f:
            f.write(b"%PDF-1.4 x")
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, _out, checks = _run(**self.basic_kwargs(files_dir=d))
        self.assertIsNone(code)
        self.assertTrue(checks["files_dir"]["pass"])


class TestFiles(_PreflightCase):
    def test_missing_entry_blocks(self):
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, out, _ = _run(**self.basic_kwargs(files=[os.path.join(self.tmp, "nope.pdf")]))
        self.assertEqual(code, 1)
        self.assertEqual(_preflight_json(out)["blocker"], "files")

    def test_existing_entries_pass(self):
        p = os.path.join(self.tmp, "real.docx")
        with open(p, "wb") as f:
            f.write(b"PK data")
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, _out, checks = _run(**self.basic_kwargs(files=[p]))
        self.assertIsNone(code)
        self.assertTrue(checks["files"]["pass"])


class TestHappyPath(_PreflightCase):
    def test_all_pass_returns_checks_without_exit(self):
        d = os.path.join(self.tmp, "resumes")
        os.makedirs(d)
        with open(os.path.join(d, "a.pdf"), "wb") as f:
            f.write(b"%PDF-1.4 x")
        with patch.dict(os.environ, _NO_ENV, clear=False):
            code, out, checks = _run(**self.basic_kwargs(files_dir=d))
        self.assertIsNone(code, "全过不应 sys.exit")
        self.assertEqual(out, "", "全过不应输出 PREFLIGHT JSON 行")
        for key in ("python", "credentials", "config", "files_dir", "files"):
            self.assertTrue(checks[key]["pass"], key)

    def test_returns_true_and_checks_directly(self):
        env = {"DINGTALK_APP_KEY": "k", "DINGTALK_APP_SECRET": "s"}
        with patch.dict(os.environ, env, clear=False):
            with redirect_stderr(io.StringIO()):
                ok, checks = run_preflight(**self.basic_kwargs())
        self.assertTrue(ok)
        self.assertIn("config", checks)


if __name__ == "__main__":
    unittest.main()
