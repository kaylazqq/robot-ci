import unittest
from unittest.mock import Mock
from frozen_builds import resolve, checkout


class FrozenBuildTests(unittest.TestCase):
    def test_missing_branch_does_not_fall_back(self):
        command = Mock(return_value=(2, ''))
        with self.assertRaises(ValueError):
            resolve('repo', 'missing', command, lambda *args: list(args), {})
        command.assert_called_once()

    def test_freezes_full_matching_ref(self):
        command = Mock(return_value=(0, 'a'*40 + '\trefs/heads/feature\n'))
        self.assertEqual(resolve('repo', 'feature', command, lambda *args: list(args), {}), 'a'*40)

    def test_failed_fetch_does_not_checkout(self):
        stream = Mock(side_effect=[0, 0, 1])
        probe = Mock()
        ok, error = checkout('job', '/owned', 'repo', 'a'*40, stream, probe, lambda *args: list(args), {})
        self.assertFalse(ok)
        self.assertEqual(stream.call_count, 3)
        probe.assert_not_called()

    def test_actual_sha_must_match(self):
        ok, error = checkout('job', '/owned', 'repo', 'a'*40, Mock(return_value=0), Mock(return_value=(0, 'b'*40)), lambda *args: list(args), {})
        self.assertFalse(ok)
        self.assertIn('mismatch', error)
