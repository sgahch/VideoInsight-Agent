from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading

import pytest

from local_helper.core import DedicatedBrowser, HelperError


def browser_profile(tmp_path):
    browser = DedicatedBrowser(tmp_path / "helper")
    browser.profile = browser.home / "sessions" / "login-cleanup-test"
    browser.profile.mkdir(parents=True)
    (browser.profile / "Cookies").write_text("synthetic-test-only", encoding="utf-8")
    return browser


def directory_link(path, destination):
    if sys.platform == "win32":
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "New-Item -ItemType Junction -Path $env:HELPER_TEST_LINK "
             "-Target $env:HELPER_TEST_DESTINATION | Out-Null"],
            env={**os.environ, "HELPER_TEST_LINK": str(path), "HELPER_TEST_DESTINATION": str(destination)},
            check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        path.symlink_to(destination, target_is_directory=True)


def test_cleanup_retries_a_missing_child_instead_of_claiming_the_profile_is_gone(tmp_path, monkeypatch):
    browser = browser_profile(tmp_path)
    remove = shutil.rmtree
    attempts = []

    def missing_child(path):
        attempts.append(path)
        if len(attempts) == 1:
            raise FileNotFoundError("A synthetic child disappeared during cleanup.")
        remove(path)

    monkeypatch.setattr("local_helper.core.shutil.rmtree", missing_child)
    monkeypatch.setattr("local_helper.core.time.sleep", lambda delay: None)
    browser.close()
    assert len(attempts) == 2
    assert not browser.profile.exists()


def test_cleanup_confirms_root_removal_even_when_rmtree_reports_success(tmp_path, monkeypatch):
    browser = browser_profile(tmp_path)
    remove = shutil.rmtree
    attempts = []

    def recreated_directory(path):
        attempts.append(path)
        if len(attempts) > 1:
            remove(path)

    monkeypatch.setattr("local_helper.core.shutil.rmtree", recreated_directory)
    monkeypatch.setattr("local_helper.core.time.sleep", lambda delay: None)
    browser.close()
    assert len(attempts) == 2
    assert not browser.profile.exists()


def test_cleanup_rechecks_the_profile_boundary_after_a_transient_error(tmp_path, monkeypatch):
    browser = browser_profile(tmp_path)
    outside = tmp_path / "not-a-helper-profile"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    attempts = []

    def moved_profile(path):
        attempts.append(path)
        browser.profile = outside
        raise PermissionError("Synthetic delayed release.")

    monkeypatch.setattr("local_helper.core.shutil.rmtree", moved_profile)
    monkeypatch.setattr("local_helper.core.time.sleep", lambda delay: None)
    with pytest.raises(HelperError, match="异常"):
        browser.close()
    assert len(attempts) == 1
    assert marker.read_text(encoding="utf-8") == "keep"


def test_cleanup_has_a_bounded_retry_window_and_preserves_the_original_error(tmp_path, monkeypatch):
    browser = browser_profile(tmp_path)
    error = PermissionError("Synthetic permanently locked profile.")
    attempts, delays = [], []

    def locked(path):
        attempts.append(path)
        raise error

    monkeypatch.setattr("local_helper.core.shutil.rmtree", locked)
    monkeypatch.setattr("local_helper.core.time.sleep", delays.append)
    with pytest.raises(HelperError, match="临时") as raised:
        browser.close()
    assert len(attempts) == 25 and len(delays) == 24
    assert sum(delays) == pytest.approx(9.6)
    assert raised.value.__cause__ is error
    assert browser.profile.is_dir()


@pytest.mark.parametrize("linked_component", ["home", "sessions", "profile"])
def test_cleanup_refuses_linked_home_sessions_or_profile(tmp_path, linked_component):
    helper, outside = tmp_path / "helper", tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    if linked_component == "home":
        profile = outside / "sessions" / "login-test"
        profile.mkdir(parents=True)
        directory_link(helper, outside)
    elif linked_component == "sessions":
        helper.mkdir()
        profile = outside / "login-test"
        profile.mkdir()
        directory_link(helper / "sessions", outside)
    else:
        (helper / "sessions").mkdir(parents=True)
        profile = outside
        directory_link(helper / "sessions" / "login-test", outside)
    browser = DedicatedBrowser(helper)
    browser.profile = helper / "sessions" / "login-test"
    try:
        with pytest.raises(HelperError, match="异常"):
            browser.close()
        assert profile.is_dir() and marker.read_text(encoding="utf-8") == "keep"
    finally:
        link = {"home": helper, "sessions": helper / "sessions", "profile": browser.profile}[linked_component]
        if sys.platform == "win32":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows file sharing semantics")
def test_cleanup_waits_for_a_real_windows_delete_lock_longer_than_two_seconds(tmp_path, monkeypatch):
    from ctypes import wintypes

    browser = browser_profile(tmp_path)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # Read/write sharing deliberately omits FILE_SHARE_DELETE.
    handle = kernel.CreateFileW(str(browser.profile / "Cookies"), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    release = threading.Event()
    closed = threading.Event()

    def unlock():
        release.wait(2.6)
        kernel.CloseHandle(handle)
        closed.set()

    thread = threading.Thread(target=unlock, daemon=True)
    thread.start()
    remove, errors = shutil.rmtree, []

    def observed_remove(path):
        try:
            remove(path)
        except OSError as error:
            errors.append(error.winerror)
            raise

    monkeypatch.setattr("local_helper.core.shutil.rmtree", observed_remove)
    try:
        browser.close()
        assert closed.is_set()
        assert errors and set(errors) == {32}
        assert not browser.profile.exists()
    finally:
        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
