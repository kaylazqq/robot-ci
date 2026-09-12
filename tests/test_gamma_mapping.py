import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamma_real


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'data').mkdir()
        with sqlite3.connect(root / 'data/robot-ci.db') as db:
            db.execute('create table environments (id text, name text, service_id text, workload_name text, jump_host text, nodes_json text)')
            for id, sid, host in [(gamma_real.ENVIRONMENT, 'governance', 'trusted'), ('multica-env','multica-server','trusted'), ('wrong-cluster','multica-server','other')]:
                db.execute('insert into environments values (?,?,?,?,?,?)', (id,'dev-gamma',sid,sid,host,json.dumps(['node1','node2'])))
        db.close()
        self.patch = patch.object(gamma_real, 'ROBOT_ROOT', root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_same_cluster_different_module_id(self):
        self.assertEqual(gamma_real.validate_selection('multica-env', ['multica-server'])['id'], 'multica-env')

    def test_name_does_not_authorize_another_cluster(self):
        with self.assertRaises(ValueError): gamma_real.environment_config('wrong-cluster')

    def test_wrong_module_rejected(self):
        with self.assertRaises(ValueError): gamma_real.validate_selection('multica-env', ['governance'])

    def test_missing_environment_rejected(self):
        self.assertFalse(gamma_real.available('missing'))

if __name__ == '__main__': unittest.main()
