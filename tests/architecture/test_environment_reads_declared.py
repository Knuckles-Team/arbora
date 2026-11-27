"""Every environment read of ``arbora/`` goes through the declared accessor (ARBORA-HOST-R010.2).

``arbora/core/environment.py`` is the only module that touches the process environment.
Any other use of ``os.environ``, ``os.getenv``, ``os.putenv``, ``os.unsetenv`` or the SDK
``setting`` reader fails this gate, so a read of an undeclared name cannot enter the
package unnoticed. The scan is by syntax tree: it follows ``import os as alias`` and
``from os import environ``, and a docstring that names a function is not a use.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "arbora"
ACCESSOR = Path("arbora/core/environment.py")
_OS_USES = frozenset({"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"})
_SDK_MODULES = frozenset({"scion_sdk.config", "arbora.core.settings"})


def _os_names(tree: ast.AST) -> set[str]:
    """The local names that are the ``os`` module."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "os"
    }


def _imports_of(node: ast.AST) -> list[str]:
    """What an import statement binds that this gate forbids."""
    if not isinstance(node, ast.ImportFrom):
        return []
    bound = [alias.name for alias in node.names]
    if node.module == "os":
        return [f"os.{name}" for name in bound if name in _OS_USES]
    if node.module in _SDK_MODULES:
        return ["setting"] if "setting" in bound else []
    return []


def _uses_of(node: ast.AST, os_names: set[str]) -> list[str]:
    """The forbidden use ``node`` is, as a short label, or an empty list."""
    if isinstance(node, ast.Attribute):
        value = node.value
        if (
            isinstance(value, ast.Name)
            and value.id in os_names
            and node.attr in _OS_USES
        ):
            return [f"os.{node.attr}"]
        if node.attr == "setting" and ast.unparse(value) in {
            "config",
            "scion_sdk.config",
        }:
            return ["setting"]
        return []
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return ["setting"] if node.func.id == "setting" else []
    return []


def raw_environment_uses(root: Path, accessor: Path = ACCESSOR) -> list[str]:
    """``path:line label`` for each raw environment use under ``root``'s ``arbora`` tree."""
    found: list[str] = []
    for path in sorted((root / "arbora").rglob("*.py")):
        relative = path.relative_to(root)
        if relative == accessor:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = _os_names(tree)
        for node in ast.walk(tree):
            for label in _imports_of(node) + _uses_of(node, names):
                found.append(f"{relative.as_posix()}:{node.lineno} {label}")
    return found


@pytest.mark.spec("ARBORA-HOST-R010.2")
def test_no_module_outside_the_accessor_reads_the_environment_raw() -> None:
    assert raw_environment_uses(ROOT) == []


@pytest.mark.spec("ARBORA-HOST-R010.2")
def test_the_gate_catches_a_planted_raw_read(tmp_path: Path) -> None:
    package = tmp_path / "arbora"
    (package / "core").mkdir(parents=True)
    (package / "core" / "environment.py").write_text(
        "import os\nVALUE = os.environ.get('ALLOWED_HERE')\n", encoding="utf-8"
    )
    planted = {
        "environ_get.py": "import os\nvalue = os.environ.get('X')\n",
        "environ_item.py": "import os\nvalue = os.environ['X']\n",
        "getenv.py": "import os\nvalue = os.getenv('X')\n",
        "aliased.py": "import os as operating_system\nvalue = operating_system.getenv('X')\n",
        "from_os.py": "from os import environ\nvalue = environ.get('X')\n",
        "putenv.py": "import os\nos.putenv('X', '1')\n",
        "sdk_setting.py": "from scion_sdk.config import setting\nvalue = setting('X')\n",
        "settings_setting.py": "from arbora.core.settings import setting\n",
        "via_module.py": "from scion_sdk import config\nvalue = config.setting('X')\n",
    }
    for name, source in planted.items():
        (package / name).write_text(source, encoding="utf-8")
    (package / "clean.py").write_text(
        '"""Mentions os.environ and setting() in a docstring only."""\n'
        "from arbora.core.environment import read_env\n"
        "value = read_env('X')\n",
        encoding="utf-8",
    )
    found = raw_environment_uses(tmp_path)
    flagged = {entry.split(":", 1)[0] for entry in found}
    assert flagged == {f"arbora/{name}" for name in planted}
    assert not any("core/environment.py" in entry for entry in found)
