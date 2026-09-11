import ast
from contextlib import nullcontext
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import gamma_e2e


class GammaBridgeTests(unittest.TestCase):
    def test_options(self):
        self.assertEqual(gamma_e2e.options({})['gamma_suites'],['E01','E02','E03'])
        for selected in ([],['E04'],['E01','E01']):
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
