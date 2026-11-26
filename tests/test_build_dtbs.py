import importlib.util
from pathlib import Path
import sys
import types
import unittest
import tempfile
import io
import urllib.error
from email.message import Message
from unittest.mock import patch


# Validation tests do not require the YAML dependency.
sys.modules.setdefault("yaml", types.ModuleType("yaml"))
spec = importlib.util.spec_from_file_location("build_dtbs", Path(__file__).parents[1] / "scripts/build_dtbs.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ConfigurationTests(unittest.TestCase):
    def kernel(self):
        return {"name": "mainline", "repository": "https://github.com/gregkh/linux.git",
                "versions": ["v6.12.119"], "selection": "all"}

    def test_valid_configuration(self):
        module.validate({"kernels": [self.kernel()]})
        self.assertEqual(module.GITHUB_REPO.fullmatch(self.kernel()["repository"]).groups(), ("gregkh", "linux"))

    def test_listed_requires_targets_unless_disabled(self):
        kernel = self.kernel() | {"selection": "listed", "targets": []}
        with self.assertRaises(ValueError):
            module.validate({"kernels": [kernel]})
        module.validate({"kernels": [kernel | {"enabled": False}]})

    def test_rejects_unsafe_target_and_duplicate_release(self):
        kernel = self.kernel() | {"selection": "listed", "targets": ["../bad.dtb"]}
        with self.assertRaises(ValueError):
            module.validate({"kernels": [kernel]})
        with self.assertRaises(ValueError):
            module.validate({"kernels": [self.kernel(), self.kernel()]})

    @patch.object(module, "run")
    def test_annotated_tag_is_peeled(self, run):
        run.return_value = "taghash\trefs/tags/v1\ncommithash\trefs/tags/v1^{}\n"
        self.assertEqual(module.resolve_commit("https://github.com/a/b.git", "v1"), "commithash")

    def test_pinned_commit_validation(self):
        kernel = self.kernel() | {"commit": "a" * 40}
        module.validate({"kernels": [kernel]})
        for invalid in ("abc", "A" * 40, 123):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                module.validate({"kernels": [kernel | {"commit": invalid}]})
        with self.assertRaises(ValueError):
            module.validate({"kernels": [kernel | {"versions": ["v1", "v2"]}]})

    @patch.object(module, "resolve_commit")
    @patch.dict(module.os.environ, {"GH_REPO": "owner/repo"})
    @patch.object(module, "download_source", side_effect=RuntimeError("stop after resolution"))
    @patch.object(module, "github_request", return_value=None)
    @patch.object(module, "find_release", return_value=None)
    def test_publish_uses_pinned_commit(self, find_release, request, download, resolve):
        kernel = self.kernel() | {"commit": "a" * 40}
        with self.assertRaisesRegex(RuntimeError, "stop after resolution"):
            module.publish(kernel, "snapshot")
        resolve.assert_not_called()
        self.assertEqual(download.call_args.args[1], "a" * 40)

    @patch.object(module, "resolve_commit", return_value="b" * 40)
    @patch.dict(module.os.environ, {"GH_REPO": "owner/repo"})
    @patch.object(module, "download_source", side_effect=RuntimeError("stop after resolution"))
    @patch.object(module, "github_request", return_value=None)
    @patch.object(module, "find_release", return_value=None)
    def test_publish_still_resolves_tags(self, find_release, request, download, resolve):
        kernel = self.kernel()
        with self.assertRaisesRegex(RuntimeError, "stop after resolution"):
            module.publish(kernel, "v1")
        resolve.assert_called_once_with(kernel["repository"], "v1")
        self.assertEqual(download.call_args.args[1], "b" * 40)


class ReleaseTests(unittest.TestCase):
    @patch.dict(module.os.environ, {"GH_TOKEN": "token"})
    @patch.object(module.time, "sleep")
    @patch.object(module.urllib.request, "urlopen")
    def test_rate_limit_waits_and_retries(self, urlopen, sleep):
        headers = Message()
        headers["Retry-After"] = "2"
        error = urllib.error.HTTPError("https://api.github.com/test", 403, "limited", headers, io.BytesIO(b"rate limit"))
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"ok": true}')
        urlopen.side_effect = [error, response]
        self.assertEqual(module.github_request("GET", "https://api.github.com/test"), {"ok": True})
        sleep.assert_called_once_with(3)

    @patch.object(module, "github_request")
    @patch.object(module, "release_assets")
    def test_resume_only_uploads_missing_matching_assets(self, release_assets, request):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.dtb"
            second = Path(tmp) / "second.dtb"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            release_assets.return_value = {"first.dtb": {"state": "uploaded", "digest": "sha256:" + module.hashlib.sha256(b"first").hexdigest()}}
            module.upload_release_assets({"id": 1, "upload_url": "https://uploads.example/assets{?name,label}"}, [first, second])
            self.assertEqual(request.call_count, 1)
            self.assertIn("name=second.dtb", request.call_args.args[1])
            first.write_bytes(b"changed")
            with self.assertRaises(RuntimeError):
                module.upload_release_assets({"id": 1, "upload_url": "https://uploads.example/assets"}, [first, second])

    @patch.object(module, "github_request")
    @patch.dict(module.os.environ, {"GH_REPO": "owner/repo"})
    def test_find_draft_without_git_tag(self, request):
        request.return_value = [{"tag_name": "mainline-v1", "draft": True}]
        self.assertTrue(module.find_release("mainline-v1")["draft"])


if __name__ == "__main__":
    unittest.main()
