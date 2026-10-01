"""Build a private local plugin or bind it to a registered ChatGPT connection.

Only known template files are copied; credentials and local configuration never
enter the package. The output directory must be new to preserve existing files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "plugins" / "rgi-openai"


def build_package(output: Path, *, app_id: str | None = None,
                  python_command: str = sys.executable, namespace: str = "private") -> Path:
    if app_id is not None and not re.fullmatch(r"plugin_asdk_app_[A-Za-z0-9_-]+", app_id):
        raise ValueError("app-id must be the plugin_asdk_app_ ID of your registered connection")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", namespace):
        raise ValueError("namespace must be 1-32 letters, digits, underscores or hyphens")
    if not python_command.strip():
        raise ValueError("python command must not be empty")
    output.mkdir(parents=True, exist_ok=False)
    plugin = output / "plugins" / "rgi-panel"
    plugin.mkdir(parents=True)
    manifest = json.loads((TEMPLATE / "plugin.json").read_text(encoding="utf-8"))
    if app_id:
        manifest["extensions"]["com.openai"]["apps"] = "./.app.json"
        (plugin / ".app.json").write_text(json.dumps({"apps": {"rgi-panel": {"id": app_id}}},
                                                    indent=2) + "\n", encoding="utf-8")
    else:
        mcp = json.loads((TEMPLATE / "mcp.json").read_text(encoding="utf-8"))
        server = mcp["mcpServers"]["rgi-panel"]
        server.update(command=python_command,
                      args=["-m", "rgi", "mcp", "--namespace", namespace])
        (plugin / "mcp.json").write_text(json.dumps(mcp, indent=2) + "\n", encoding="utf-8")
    (plugin / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    skill = plugin / "skills" / "rgi-panel" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text((TEMPLATE / "skills" / "rgi-panel" / "SKILL.md").read_text(encoding="utf-8"),
                     encoding="utf-8")
    marketplace = output / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text(json.dumps({
        "name": "rgi-private-testing",
        "interface": {"displayName": "RGI Private Testing"},
        "plugins": [{"name": "rgi-panel", "source": {"source": "local", "path": "./plugins/rgi-panel"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                     "category": "Productivity"}],
    }, indent=2) + "\n", encoding="utf-8")
    archive = output / "rgi-panel.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path in sorted(plugin.rglob("*")):
            if path.is_file():
                package.write(path, path.relative_to(plugin).as_posix())
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    parser.add_argument("--app-id", help="registered tunnel-backed ChatGPT connection ID")
    parser.add_argument("--python", default=sys.executable, help="local MCP Python executable")
    parser.add_argument("--namespace", default="private", help="local adapter namespace")
    args = parser.parse_args()
    try:
        archive = build_package(args.output, app_id=args.app_id,
                                python_command=args.python, namespace=args.namespace)
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    print(f"Built {archive}")
    print(f"Marketplace root: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
