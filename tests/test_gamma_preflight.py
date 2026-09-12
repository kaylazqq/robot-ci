import ast
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamma_real


class GammaPreflightTests(unittest.TestCase):
    def check(self, options, ready=False):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8'))
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.Try) and
                    any(isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'optional_steps'
                        for t in s.targets) for s in n.body))
        function = ast.parse('def check():\n    pass\n').body[0]
        function.body = [node, ast.Return(value=ast.Constant('accepted'))]
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        handler = Mock()
        scope = {'CFG': {}, 'data': {'optional_steps': options}, '_normalize_optional_steps': lambda v: v, 'self': handler,
                 'items': [{'service_id': 'agent-governance-gw'}]}
        exec(compile(module, 'gamma-preflight', 'exec'), scope)
        with patch.object(gamma_real, 'available', return_value=ready), patch.object(gamma_real, 'validate_selection'):
            return scope['check'](), handler

    def test_configured_gamma_is_allowed(self):
        result, handler = self.check({'gamma_test': True, 'gamma_mode': 'browser-e2e', 'environment_id': 'a5932430eb2f'}, ready=True)
        self.assertEqual(result, 'accepted')
        handler._json.assert_not_called()

    def test_real_environment_test_fails_before_job_creation(self):
        for environment in ('', 'a5932430eb2f', 'another-environment'):
            result, handler = self.check({'gamma_test': True, 'gamma_mode': 'browser-e2e', 'environment_id': environment})
            self.assertIsNone(result)
            self.assertEqual(handler._json.call_args.args[0], 400)
            self.assertIn('未创建构建任务', handler._json.call_args.args[1]['error'])

    def test_build_only_and_existing_deploy_only_are_unchanged(self):
        for options in ({}, {'gamma_test': False, 'gamma_deploy': True, 'environment_id': 'gamma'}):
            result, handler = self.check(options)
            self.assertEqual(result, 'accepted')
            handler._json.assert_not_called()

    def test_repository_tests_do_not_require_browser_driver(self):
        for mode in ({}, {'gamma_mode': 'repository'}):
            result, handler = self.check({'gamma_test': True, 'environment_id': 'gamma', **mode})
            self.assertEqual(result, 'accepted')
            handler._json.assert_not_called()


if __name__ == '__main__':
    unittest.main()
