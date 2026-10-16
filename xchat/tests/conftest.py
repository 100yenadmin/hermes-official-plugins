"""Test bootstrap: import the plugin directory as the package ``xchat_plugin``.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative imports in ``__init__.py`` resolve; tests mirror that under a stable alias. Hermes core
(``gateway``, ``hermes_cli``, ...) must be importable: run with a hermes-agent checkout on ``PYTHONPATH``.

``Platform("xchat")`` only resolves for a platform the registry knows, so the plugin's real
registration runs up front exactly as the loader would.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "xchat_plugin"
_ROOT = Path(__file__).resolve().parents[1]

# ``python -m pytest`` from the repo root puts the root on sys.path, where this plugin's modules could
# shadow core packages when core is imported.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]


def _load_plugin_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def _ensure_platform_registered(plugin) -> None:
    from gateway.platform_registry import PlatformEntry, platform_registry

    if platform_registry.is_registered("xchat"):
        return
    captured: dict = {}

    class _Ctx:
        class manifest:
            name = "xchat"

        def register_platform(self, **kwargs):
            captured.update(kwargs)

        def register_cli_command(self, **kwargs):
            pass

    plugin.register(_Ctx())
    platform_registry.register(PlatformEntry(source="plugin", **captured))


_ensure_platform_registered(_load_plugin_package())


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    """Every test gets its own profile home: key blobs, cursors and .env land there, never in the real one."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in list(__import__("os").environ):
        if name.startswith("XCHAT_"):
            monkeypatch.delenv(name, raising=False)
    return home


class _RepoRootIsNotATestPackage:
    """The plugin dir *is* a package (its ``__init__.py`` uses relative imports), so pytest would
    collect it as a test ``Package`` and import that ``__init__.py`` standalone, where the relative
    imports cannot resolve. Collect it as a plain directory instead."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("xchat-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "xchat-repo-root")
