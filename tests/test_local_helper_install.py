from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from local_helper.core import HelperError
import local_helper.windows as installer


@pytest.fixture
def registry(monkeypatch, tmp_path):
    values = {}

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def create(hive, path):
        assert hive == "current-user"
        assert path.startswith("Software\\Classes\\videoinsight-login")
        values.setdefault(path, {})
        return Key(path)

    def open_key(hive, path):
        if path not in values:
            raise FileNotFoundError
        return Key(path)

    def set_value(key, name, _, kind, value):
        values[key.path][name] = value

    def delete(hive, path):
        if path not in values:
            raise FileNotFoundError
        del values[path]

    fake = SimpleNamespace(
        HKEY_CURRENT_USER="current-user", REG_SZ=1, CreateKey=create, OpenKey=open_key,
        SetValueEx=set_value, DeleteKey=delete,
        QueryValueEx=lambda key, name: (values[key.path][name], 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(installer, "private_home", lambda: tmp_path / "local")
    binary = tmp_path / "download" / "VideoInsightLoginHelper.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"MZ-synthetic-install-test-not-executable")
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    return values, binary


def test_install_only_registers_current_user_protocol_with_quoted_executable(registry):
    values, source = registry
    destination = installer.install()
    assert destination.read_bytes() == source.read_bytes()
    assert values[installer.REGISTRY_PATH]["URL Protocol"] == ""
    assert values[installer.REGISTRY_PATH + "\\shell\\open\\command"][""] == f'"{destination}" "%1"'
    assert all("Run" not in key and "HKEY_LOCAL_MACHINE" not in key for key in values)


def test_remove_only_unregisters_own_protocol_and_keeps_executable(registry):
    values, _ = registry
    destination = installer.install()
    installer.uninstall()
    assert not values and destination.is_file()


def test_remove_refuses_another_programs_registration(registry):
    values, _ = registry
    installer.install()
    values[installer.REGISTRY_PATH + "\\shell\\open\\command"][""] = '"C:\\Other\\helper.exe" "%1"'
    before = {key: dict(value) for key, value in values.items()}
    with pytest.raises(HelperError):
        installer.uninstall()
    assert values == before


def test_source_mode_cannot_register_python_as_protocol_handler(registry, monkeypatch):
    values, _ = registry
    monkeypatch.setattr(sys, "frozen", False)
    with pytest.raises(HelperError):
        installer.install()
    assert not values


def test_current_uninstaller_recognizes_its_previous_version(registry):
    values, _ = registry
    installer.install()
    previous = installer.private_home() / "1.0.0" / "VideoInsightLoginHelper.exe"
    previous.parent.mkdir(parents=True, exist_ok=True)
    previous.write_bytes(b"old-synthetic-executable")
    values[installer.REGISTRY_PATH + "\\shell\\open\\command"][""] = f'"{previous}" "%1"'
    installer.uninstall()
    assert not values and previous.is_file()


def test_install_upgrade_preserves_old_executable_and_replaces_only_own_protocol(registry):
    values, _ = registry
    previous = installer.private_home() / "1.0.0" / "VideoInsightLoginHelper.exe"
    previous.parent.mkdir(parents=True, exist_ok=True)
    previous.write_bytes(b"old-synthetic-executable")
    values[installer.REGISTRY_PATH + "\\shell\\open\\command"] = {"": f'"{previous}" "%1"'}
    destination = installer.install()
    assert destination != previous and previous.read_bytes() == b"old-synthetic-executable"
    assert values[installer.REGISTRY_PATH + "\\shell\\open\\command"][""] == f'"{destination}" "%1"'
