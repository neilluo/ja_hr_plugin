# -*- coding: utf-8 -*-
"""周期租约回归：refine_loop.acquire_lock 的占用/释放/过期/--force 夺回/双链独立语义。

防的事故：两个消费周期并发切批双写（2026-09-27 实测竞态；现触发场景 = 连续上传各注册
即时任务，撞租约者秒退）。纯本地文件操作，不触网。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
import refine_loop  # noqa: E402


class TestRefineLease(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ja_lease_test_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_acquire_then_second_refused(self):
        p = refine_loop.acquire_lock(self.root, "resume")
        self.assertTrue(p and os.path.exists(p))
        self.assertIsNone(refine_loop.acquire_lock(self.root, "resume"),
                          "活租约期内第二个周期必须被拒")

    def test_release_allows_reacquire(self):
        p = refine_loop.acquire_lock(self.root, "resume")
        refine_loop.release_lock(p)
        self.assertIsNotNone(refine_loop.acquire_lock(self.root, "resume"))

    def test_stale_lease_expires(self):
        p = refine_loop.acquire_lock(self.root, "resume", stale_after=1)
        os.utime(p, (time.time() - 2, time.time() - 2))
        self.assertIsNotNone(refine_loop.acquire_lock(self.root, "resume", stale_after=1),
                             "过期租约（崩溃遗留）必须可被下一周期接管")

    def test_force_reclaims_own_lease(self):
        self.assertIsNotNone(refine_loop.acquire_lock(self.root, "resume"))
        self.assertIsNotNone(refine_loop.acquire_lock(self.root, "resume", force=True),
                             "--force 同周期重切批必须能夺回自有租约")

    def test_chains_independent(self):
        self.assertIsNotNone(refine_loop.acquire_lock(self.root, "resume"))
        self.assertIsNotNone(refine_loop.acquire_lock(self.root, "job"),
                             "resume/job 队列不相交，两链租约必须独立可并行")
        self.assertNotEqual(refine_loop.lock_path(self.root, "resume"),
                            refine_loop.lock_path(self.root, "job"))

    def test_lease_payload(self):
        p = refine_loop.acquire_lock(self.root, "resume")
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["pid"], os.getpid())
        self.assertIsInstance(d["ts"], int)

    def test_release_none_or_missing_is_quiet(self):
        refine_loop.release_lock(None)                       # 幂等：未持锁也要安静
        refine_loop.release_lock(os.path.join(self.root, "nope.lock"))  # 不存在也安静

    def test_renew_pushes_mtime_and_keeps_lease_alive(self):
        # A（续租）：长跑流程在 merge 处续租，把判活窗口随工作推进滚动，
        # 防 stale_after 窗口被"最慢 subagent 批次墙钟"耗光后被并存周期接管清产物。
        p = refine_loop.acquire_lock(self.root, "resume")
        os.utime(p, (time.time() - (refine_loop.STALE_AFTER_S - 5),) * 2)  # 逼近过期
        self.assertTrue(refine_loop.renew_lock(self.root, "resume"))
        self.assertLess(abs(time.time() - os.path.getmtime(p)), 2, "续租必须把 mtime 推回当下")
        self.assertIsNone(refine_loop.acquire_lock(self.root, "resume"),
                          "续租后原周期仍持锁，第二周期必须被拒")

    def test_renew_never_creates_a_lock(self):
        # 续租不是拿锁：锁不存在（已释放/从未获取）时返回 False 且不新建，
        # 否则会把"上一周期已释"伪装成"有人在跑"，毒化下一周期。
        self.assertFalse(refine_loop.renew_lock(self.root, "resume"))
        self.assertFalse(os.path.exists(refine_loop.lock_path(self.root, "resume")),
                         "renew 不得凭空创建锁文件")


if __name__ == "__main__":
    unittest.main()
