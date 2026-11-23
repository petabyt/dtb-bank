import importlib.util
from pathlib import Path
import sys
import types
import unittest
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


if __name__ == "__main__":
    unittest.main()
