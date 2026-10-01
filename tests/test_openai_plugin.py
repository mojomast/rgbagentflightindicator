"""Private package output and registered-app wiring checks."""

import json
from pathlib import Path
import runpy
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent.parent
build_package = runpy.run_path(str(ROOT / "tools" / "package_openai_plugin.py"))["build_package"]


class TestOpenAIPlugin(unittest.TestCase):
    def test_local_package_has_portable_mcp_and_resolvable_marketplace(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "package"
            archive = build_package(output, python_command="python", namespace="test")
            marketplace = json.loads((output / ".agents/plugins/marketplace.json").read_text())
            plugin = output / marketplace["plugins"][0]["source"]["path"]
            self.assertTrue((plugin / "plugin.json").is_file())
            with zipfile.ZipFile(archive) as package:
                self.assertEqual(set(package.namelist()),
                                 {"plugin.json", "mcp.json", "skills/rgi-panel/SKILL.md"})
                config = json.loads(package.read("mcp.json"))
                server = config["mcpServers"]["rgi-panel"]
                self.assertEqual(server["type"], "stdio")
                self.assertEqual(server["args"], ["-m", "rgi", "mcp", "--namespace", "test"])

    def test_registered_connection_replaces_local_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = build_package(Path(temp) / "package", app_id="plugin_asdk_app_test")
            with zipfile.ZipFile(archive) as package:
                self.assertNotIn("mcp.json", package.namelist())
                manifest = json.loads(package.read("plugin.json"))
                extension = manifest["extensions"]["com.openai"]
                mapping = json.loads(package.read(extension["apps"].removeprefix("./")))
                self.assertEqual(mapping["apps"]["rgi-panel"]["id"], "plugin_asdk_app_test")
                self.assertIn("skills/rgi-panel/SKILL.md", package.namelist())

    def test_invalid_app_id_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "package"
            with self.assertRaises(ValueError):
                build_package(output, app_id="not-a-registered-app")
            self.assertFalse(output.exists())

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            user_file = output / "user-file"
            user_file.write_text("preserve")
            with self.assertRaises(FileExistsError):
                build_package(output)
            self.assertEqual(user_file.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
