"""部署工具的隔离验收：身份不匹配、配置保护、失败现场与仅重建应用"""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("apply_image", Path(__file__).parents[1] / "apply-image.py")
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.compose = self.root / "deploy" / "compose.yaml"
        self.compose.parent.mkdir()
        self.original = (Path(__file__).parents[1] / "compose.yaml").read_bytes()
        self.compose.write_bytes(self.original)
        self.manifest = {
            "schema_version": 1,
            "repository": APP.DEFAULT_REPOSITORY,
            "git_sha": "a" * 40,
            "version": "3.43.1+git." + "a" * 12,
            "build_type": "source",
            "platform": "linux/amd64",
            "image": "ghcr.io/" + APP.DEFAULT_REPOSITORY + "@sha256:" + "b" * 64,
        }
        self.calls = []
        self.bad_image = False
        self.bad_runtime = False
        self.bad_infrastructure = False
        self.failed_start = False
        self.changed_compose = False
        self.base = {"services": {
            "postgres": {"image": "postgres:18", "volumes": ["unchanged-database"]},
            "redis": {"image": "redis:8", "volumes": ["unchanged-cache"]},
            APP.SERVICE: {"image": "previous", "environment": {
                "CPR_UPDATE_REPOSITORY": "zyycn/codex-proxy-rs",
                "CPR_ENABLE_SELF_RESTART": "true", "OTHER": "retained"},
                "volumes": ["unchanged-accounts"], "ports": ["127.0.0.1:8080:8080"]},
        }}

    def docker(self, args, **kwargs):
        self.calls.append(args)
        if "config" in args:
            result = copy.deepcopy(self.base)
            if Path(args[args.index("-f") + 1]) != self.compose:
                result["services"][APP.SERVICE]["image"] = self.manifest["image"]
                result["services"][APP.SERVICE]["environment"].update(
                    CPR_UPDATE_REPOSITORY=APP.DEFAULT_REPOSITORY, CPR_ENABLE_SELF_RESTART="false")
                if self.bad_infrastructure:
                    result["services"]["postgres"]["image"] = "different-database"
            return json.dumps(result)
        if "ps" in args:
            return "running-container"
        if args[1] == "inspect":
            return json.dumps([{"Image": "sha256:previous", "Config": {"Image": "previous-reference"}}])
        if args[1:3] == ["image", "inspect"]:
            if self.changed_compose:
                self.compose.write_bytes(self.original + b"# concurrent edit\n")
            return json.dumps([{"Os": "linux", "Architecture": "amd64", "Config": {"Labels": {
                "org.opencontainers.image.revision": "wrong" if self.bad_image else self.manifest["git_sha"],
                "org.opencontainers.image.version": self.manifest["version"],
                "org.opencontainers.image.source": "https://github.com/" + APP.DEFAULT_REPOSITORY,
                "io.codex-proxy.build-type": "source",
            }}}])
        if "up" in args and self.failed_start:
            raise RuntimeError("application did not become healthy")
        if "postgres" in args and "exec" in args:
            return "private-admin-key"
        if args[-1].endswith("/system/version"):
            return json.dumps({"data": {"gitSha": "wrong" if self.bad_runtime else self.manifest["git_sha"], "version": self.manifest["version"]}})
        if args[-1].endswith("/update/detail"):
            return json.dumps({"data": {"buildType": "source", "updateSupported": False}})
        return ""

    def records(self):
        return list((self.root / ".runtime" / "deployments").glob("*/result.json"))

    @patch.object(APP, "run")
    def test_dry_run_keeps_files_and_containers_unchanged(self, run):
        run.side_effect = self.docker
        self.assertFalse(APP.deploy(self.manifest, self.compose)["applied"])
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertFalse((self.root / ".runtime").exists())
        self.assertFalse(any("pull" in call or "up" in call for call in self.calls))

    @patch.object(APP, "run")
    def test_success_updates_only_app_and_preserves_recovery_record(self, run):
        run.side_effect = self.docker
        result = APP.deploy(self.manifest, self.compose, apply=True)
        self.assertTrue(result["applied"])
        self.assertIn(self.manifest["image"].encode(), self.compose.read_bytes())
        ups = [call for call in self.calls if "up" in call]
        self.assertEqual(len(ups), 1)
        self.assertEqual(ups[0][-1], APP.SERVICE)
        self.assertIn("--no-deps", ups[0])
        record = self.records()[0]
        self.assertEqual((record.parent / "compose.before.yaml").read_bytes(), self.original)
        self.assertEqual(json.loads(record.read_text())["previous_image"], "sha256:previous")
        self.assertNotIn("private-admin-key", record.read_text())
        self.assertNotIn("private-admin-key", json.dumps(self.calls))

    @patch.object(APP, "run")
    def test_foreign_image_is_rejected_before_compose_changes(self, run):
        run.side_effect = self.docker
        self.bad_image = True
        with self.assertRaisesRegex(ValueError, "镜像身份"):
            APP.deploy(self.manifest, self.compose, apply=True)
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertFalse(any("up" in call for call in self.calls))

    @patch.object(APP, "run")
    def test_infrastructure_change_is_rejected_before_pull(self, run):
        run.side_effect = self.docker
        self.bad_infrastructure = True
        with self.assertRaisesRegex(ValueError, "部署变化"):
            APP.deploy(self.manifest, self.compose, apply=True)
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertFalse(any("pull" in call for call in self.calls))

    @patch.object(APP, "run")
    def test_start_failure_keeps_target_and_does_not_run_old_binary(self, run):
        run.side_effect = self.docker
        self.failed_start = True
        with self.assertRaisesRegex(RuntimeError, "healthy"):
            APP.deploy(self.manifest, self.compose, apply=True)
        self.assertIn(self.manifest["image"].encode(), self.compose.read_bytes())
        self.assertEqual(sum("up" in call for call in self.calls), 1)
        self.assertTrue(json.loads(self.records()[0].read_text())["failed"])
        self.assertEqual((self.records()[0].parent / "compose.before.yaml").read_bytes(), self.original)

    @patch.object(APP, "run")
    def test_concurrent_file_edit_is_preserved(self, run):
        run.side_effect = self.docker
        self.changed_compose = True
        with self.assertRaisesRegex(ValueError, "其他操作"):
            APP.deploy(self.manifest, self.compose, apply=True)
        self.assertTrue(self.compose.read_bytes().endswith(b"# concurrent edit\n"))

    @patch.object(APP, "run")
    def test_incorrect_running_process_fails_acceptance(self, run):
        run.side_effect = self.docker
        self.bad_runtime = True
        with self.assertRaisesRegex(ValueError, "运行进程"):
            APP.deploy(self.manifest, self.compose, apply=True)
        self.assertFalse(json.loads(self.records()[0].read_text())["applied"])

    def test_mutable_tags_and_other_publishers_are_rejected(self):
        for image in ["ghcr.io/" + APP.DEFAULT_REPOSITORY + ":dev", "ghcr.io/zyycn/codex-proxy-rs@sha256:" + "b" * 64]:
            data = dict(self.manifest, image=image)
            path = self.root / "manifest.json"
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "固定镜像摘要"):
                APP.load_manifest(path, APP.DEFAULT_REPOSITORY)


if __name__ == "__main__":
    unittest.main()
