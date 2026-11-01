import pytest

from codee.admin_service import AdminService
from codee.lib.plugins import LoadedPlugins


@pytest.fixture(autouse=True)
def no_installed_plugins(monkeypatch):
    """Core tests run as if no plugin were installed.

    Whatever plugins the environment has (``uv sync --all-packages`` installs
    the ones in plugins/) must not change what the core tests see; a test that
    wants plugins sets ``service.plugins`` itself.
    """
    monkeypatch.setattr(AdminService, "plugins", LoadedPlugins())
