import ast
from contextlib import nullcontext
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import gamma_e2e


class GammaBridgeTests(unittest.TestCase):
    def test_repository_gamma_remains_default(self):
        import re
        import gamma_real
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8'))
        tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in
                     ('_normalize_optional_steps', 'maybe_run_gamma_after_build')]
        scope = {'Any': object, 're': re, 'LOG_DIR': Path('/unused'),
                 '_job_copy': lambda _: {'optional_steps': {'environment_id': 'repo-env', 'gamma_test': True}},
                 '_job_service_ids': lambda _: ['service'], 'job_cancel_requested': lambda _: False,
                 'set_job': Mock(),
                 'get_environment': Mock(return_value={'name': 'repo-env', 'service_id': 'service'}),
                 'log_substep': lambda _: nullcontext(), 'load_services': lambda: [{'id': 'service'}],
                 'repo_dir': lambda *_: Path('/unused'), 'record_gamma_run': Mock(),
                 'run_gamma_tests': Mock(return_value={'status': 'passed', 'total': 1, 'passed': 1})}
        exec(compile(tree, 'server.py', 'exec'), scope)
        with patch.object(gamma_real, 'run') as browser:
            self.assertEqual(scope['maybe_run_gamma_after_build']('test', [{'service_id': 'service', 'ok': True}]), (True, ''))
            scope['run_gamma_tests'].assert_called_once()
            scope['record_gamma_run'].assert_called_once()
            browser.assert_not_called()
        with self.assertRaises(ValueError):
            scope['_normalize_optional_steps']({'gamma_test': True, 'gamma_mode': 'unknown'})

    def test_real_environment_normalization_preserves_selected_suites(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'server.py').read_text(encoding='utf-8'))
        tree.body = [n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name == '_normalize_optional_steps']
        scope = {'Any':object}
        exec(compile(tree,'server.py','exec'),scope)
        for suites in (['E04'], ['E01','E02','E03','E04','E05','E06']):
            result = scope['_normalize_optional_steps']({'environment_id':'multica-dev-gamma','gamma_test':True,'gamma_mode':'browser-e2e','gamma_suites':suites})
            self.assertEqual(result['gamma_suites'], suites)

    def test_configured_gamma_calls_real_driver_and_propagates_failure(self):
        import gamma_real
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8'))
        tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in
                     ('_normalize_optional_steps', 'maybe_run_gamma_after_build')]
        env = {'Any': object, '_job_copy': lambda _: {'optional_steps': {
            'environment_id': 'a5932430eb2f', 'gamma_test': True, 'gamma_deploy': True, 'gamma_mode': 'browser-e2e'}},
            '_job_service_ids': lambda _: ['agent-governance-gw'],
            'job_cancel_requested': lambda _: False, 'set_job': Mock(), 'release_build_slot': Mock(),
            'get_environment': Mock(return_value={'name': 'dev-gamma'}),
            'append_job_log': Mock(), 'log_substep': lambda _: nullcontext(), 'cce_rollout': Mock()}
        exec(compile(tree, 'server.py', 'exec'), env)
        with patch.object(gamma_real, 'available', return_value=True), patch.object(gamma_real, 'run', return_value=(False, 'E02 failed')) as run:
            self.assertEqual(env['maybe_run_gamma_after_build']('test', [{'ok': True}]), (False, 'E02 failed'))
            run.assert_called_once()
            env['cce_rollout'].deploy_job_results.assert_not_called()
            env['release_build_slot'].assert_called_once_with('test')

    def test_unconfigured_cce_test_blocks_before_deployment(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text())
        tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in
                     ('_normalize_optional_steps', 'maybe_run_gamma_after_build')]
        deploy = Mock()
        env = {'Any': object, '_job_copy': lambda _: {'optional_steps': {
            'environment_id': 'real-gamma', 'gamma_test': True, 'gamma_deploy': True, 'gamma_mode': 'browser-e2e'}},
            '_job_service_ids': lambda _: ['agent-governance-gw'],
            'job_cancel_requested': lambda _: False, 'set_job': Mock(),
            'get_environment': Mock(return_value={'name': 'dev-gamma'}),
            'append_job_log': Mock(), 'cce_rollout': Mock(deploy_job_results=deploy)}
        exec(compile(tree, 'server.py', 'exec'), env)
        ok, error = env['maybe_run_gamma_after_build']('test', [])
        self.assertFalse(ok)
        self.assertIn('未部署镜像', error)
        deploy.assert_not_called()

    def test_options(self):
        self.assertEqual(gamma_e2e.options({})['gamma_suites'],['E01','E02','E03'])
        self.assertEqual(gamma_e2e.options({'gamma_suites':['E04','E05','E06']})['gamma_suites'], ['E04','E05','E06'])
        for selected in ([],['E07'],['E01','E01']):
            with self.assertRaises(ValueError):gamma_e2e.options({'gamma_suites':selected})

    def test_ci_target_never_calls_cce(self):
        # Load the orchestration functions without starting the server or touching its DB.
        tree=ast.parse((Path(__file__).resolve().parents[1]/'server.py').read_text())
        tree.body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('_normalize_optional_steps','maybe_run_gamma_after_build')]
        env={'Any':object,'_job_copy':lambda _: {'optional_steps':{'environment_id':'ci-e2e','gamma_test':True}},
             'job_cancel_requested':lambda _:False,'set_job':Mock(),'release_build_slot':Mock(),
             'log_substep':lambda _:nullcontext(),'get_environment':Mock(),'append_job_log':Mock()}
        exec(compile(tree,'server.py','exec'),env)
        with patch.object(gamma_e2e,'run',return_value=(True,'')) as run:
            self.assertEqual(env['maybe_run_gamma_after_build']('test',[]),(True,''))
            run.assert_called_once();env['get_environment'].assert_not_called();env['release_build_slot'].assert_called_once_with('test')
        with patch.object(gamma_e2e,'run',side_effect=RuntimeError('offline')):
            ok,error=env['maybe_run_gamma_after_build']('test',[])
            self.assertFalse(ok);self.assertIn('offline',error)

    def test_repeated_poll_does_not_submit_again(self):
        body={'suite_ids':['E01']}
        with patch.object(gamma_e2e,'prepare',return_value=body),patch.object(gamma_e2e,'request',side_effect=[
            {'id':'gamma-test','web_path':'/batches/gamma-test'},
            {'status':'running','stages':[]},
            {'status':'completed','conclusion':'failure','summary':'assertion failed'}]) as request,patch.object(gamma_e2e.time,'sleep'):
            ok,error=gamma_e2e.run('test',[],{},lambda _:None,lambda _:None,lambda:False)
            self.assertFalse(ok);self.assertEqual(error,'assertion failed')
            self.assertEqual(sum(c.args[0]=='/internal/artifact-batches' for c in request.call_args_list),1)

if __name__=='__main__':unittest.main()
