"""The single-instance lock: one watcher per machine, and no friendly fire.

os.kill(pid, 0) looks like a liveness probe, but on Windows it is not a signal at
all - it is TerminateProcess. The probe would kill the watcher it was asking
about, which is precisely what happened the first time this was written. These
tests pin the behaviour that replaced it.
"""

import os
import tempfile
import unittest

from rgi.watchers.opencode import _pid_alive, acquire_single_instance, release_instance


class TestLock(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="rgi-lock-", suffix=".lock")
        os.close(fd)
        os.remove(self.path)

    def tearDown(self):
        release_instance(self.path)

    def test_this_process_is_alive_and_a_nonsense_pid_is_not(self):
        self.assertTrue(_pid_alive(os.getpid()))
        self.assertFalse(_pid_alive(999999))

    def test_first_caller_takes_it_and_the_second_is_told_who_has_it(self):
        self.assertIsNone(acquire_single_instance(self.path))
        holder = acquire_single_instance(self.path)
        self.assertIsNotNone(holder)
        self.assertIn(str(os.getpid()), holder)

    def test_a_stale_lock_is_taken_over(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("999999 (since whenever)")
        self.assertIsNone(acquire_single_instance(self.path))

    def test_releasing_frees_it_for_the_next_agent(self):
        acquire_single_instance(self.path)
        release_instance(self.path)
        self.assertIsNone(acquire_single_instance(self.path))

    def test_an_unreadable_lock_does_not_block_startup(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("not a pid at all")
        self.assertIsNone(acquire_single_instance(self.path))


if __name__ == "__main__":
    unittest.main()
