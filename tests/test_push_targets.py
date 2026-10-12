import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class PushTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_patch = patch.object(server, "DB_PATH", root / "robot-ci.db")
        self.users_patch = patch.object(server, "USERS_PATH", root / "users.json")
        self.key_patch = patch.object(server, "REGISTRY_SECRET_KEY_PATH", root / "registry-target.key")
        self.db_patch.start()
        self.users_patch.start()
        self.key_patch.start()
        server.ensure_default_users()

    def tearDown(self) -> None:
        self.key_patch.stop()
        self.users_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_parse_docker_login_accepts_credentials_without_shell_execution(self) -> None:
        username, password, registry = server.parse_docker_login_command(
            "docker login -u cn-southwest-2@example -p 'temporary secret' swr.example.com"
        )
        self.assertEqual("cn-southwest-2@example", username)
        self.assertEqual("temporary secret", password)
        self.assertEqual("swr.example.com", registry)

        for command in (
            "bash -lc 'docker login -u user -p pass swr.example.com'",
            "docker login -u user -p pass swr.example.com && whoami",
            "docker login --password-stdin -u user swr.example.com",
            "docker login -u user -p pass swr.example.com/team",
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                server.parse_docker_login_command(command)

    def test_target_secret_is_encrypted_and_template_can_select_target(self) -> None:
        password = "a-plain-text-password"
        target, error = server.save_registry_target(
            {
                "service_id": "echoagent-frontend",
                "name": "第二账号",
                "namespace": "another_org",
                "login_command": f"docker login -u build-user -p {password} swr.example.com",
            },
            "l30042018",
        )
        self.assertEqual("", error)
        self.assertIsNotNone(target)
        self.assertNotIn("username", target)
        self.assertNotIn("password", target)

        conn = server._connect_db()
        try:
            row = conn.execute(
                "SELECT username,password_cipher FROM registry_targets WHERE id=?", (target["id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual("build-user", row["username"])
        self.assertNotIn(password, row["password_cipher"])
        private = server.get_registry_target(target["id"], include_secret=True)
        self.assertEqual(password, private["password"])

        duplicate, duplicate_error = server.save_registry_target(
            {
                "service_id": "echoagent-frontend",
                "name": "第二账号",
                "namespace": "another_org",
                "login_command": f"docker login -u build-user -p {password} swr.example.com",
            },
            "l30042018",
        )
        self.assertIsNone(duplicate)
        self.assertEqual("同一微服务下已存在同名推送配置，请直接选择已有配置", duplicate_error)

        template = next(
            item
            for item in server.list_pipeline_templates("l30042018", "echoagent-frontend")
            if item["kind"] == "personal"
        )
        updated, template_error = server.update_pipeline_template(
            "l30042018",
            template["id"],
            {**template, "push_target_id": target["id"]},
        )
        self.assertEqual("", template_error)
        self.assertEqual(target["id"], updated["push_target_id"])
        self.assertEqual("推送目标仍被流水线使用，不能删除", server.delete_registry_target(target["id"]))

    def test_target_login_uses_isolated_docker_config(self) -> None:
        target = {
            "name": "生产账号",
            "registry": "swr.example.com",
            "namespace": "public_ai",
            "username": "build-user",
            "password": "secret",
        }
        config_dir = Path(self.tmp.name) / "docker-auth"
        with patch.object(server, "docker_login_credentials", return_value=(True, "Login Succeeded")) as login, \
                patch.object(server, "docker_cmd", return_value=(1, "manifest unknown")) as docker, \
                patch.object(server, "append_job_log"), patch.object(server, "set_job"):
            self.assertTrue(server.ensure_registry_target_login("job123", target, config_dir))
        login.assert_called_once_with(
            username="build-user",
            password="secret",
            registry="swr.example.com",
            config_dir=config_dir,
        )
        self.assertEqual(config_dir, docker.call_args.kwargs["config_dir"])
        self.assertEqual(
            "swr.example.com/public_ai/robot-ci-auth-probe-does-not-exist",
            docker.call_args.args[-1],
        )


if __name__ == "__main__":
    unittest.main()
