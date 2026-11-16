from __future__ import annotations

import shutil
import sys
from pathlib import Path

from local_helper.core import HelperError, SCHEME, VERSION, private_home


REGISTRY_PATH = "Software\\Classes\\" + SCHEME


def install() -> Path:
    import winreg

    if not getattr(sys, "frozen", False):
        raise HelperError("请使用已打包的 Windows 安装程序。")
    source = Path(sys.executable).resolve()
    destination = private_home() / VERSION / "VideoInsightLoginHelper.exe"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source != destination.resolve():
        try:
            shutil.copy2(source, destination)
        except PermissionError:
            raise HelperError("助手正在运行，请先关闭其他助手窗口，再重试安装。") from None
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:VideoInsight Login Helper")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH + "\\DefaultIcon") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, '"' + str(destination) + '",0')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH + "\\shell\\open\\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, '"' + str(destination) + '" "%1"')
    return destination


def uninstall():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH + "\\shell\\open\\command") as key:
            command = winreg.QueryValueEx(key, "")[0]
    except FileNotFoundError:
        return
    expected = {
        '"' + str(private_home() / version / "VideoInsightLoginHelper.exe") + '" "%1"'
        for version in {VERSION, "1.0.0"}
    }
    if command not in expected:
        raise HelperError("检测到其他版本的协议关联，未修改该关联。")
    for suffix in ("\\shell\\open\\command", "\\shell\\open", "\\shell", "\\DefaultIcon", ""):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH + suffix)
        except FileNotFoundError:
            pass
    # Do not delete a running executable or touch any browser's user profile.
