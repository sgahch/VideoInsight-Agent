from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk
from urllib.parse import urlsplit

from local_helper.core import (
    Cancelled, HelperClient, HelperError, SITE, VERSION, capture_and_save, parse_launch_url,
)
from local_helper.windows import install, uninstall


class HelperWindow:
    def __init__(self, launch: str | None):
        self.root = tk.Tk()
        self.root.title("VideoInsight 登录助手")
        self.root.geometry("540x300")
        self.root.minsize(480, 300)
        self.root.configure(bg="#f4f6f8")
        ttk.Style(self.root).configure("TButton", font=("Microsoft YaHei UI", 10), padding=(10, 6))
        self.events = queue.Queue()
        self.cancelled = threading.Event()
        self.busy = False
        self.closed = False
        self.client = None
        self.claim = None
        self.title = tk.StringVar(value="VideoInsight 本地登录助手")
        self.description = tk.StringVar(value="仅授权当前网站账号，不读取日常浏览器。")
        self.message = tk.StringVar(value="")
        panel = ttk.Frame(self.root, padding=24)
        self.panel = panel
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, textvariable=self.title, font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            panel, textvariable=self.description, font=("Microsoft YaHei UI", 10), wraplength=470,
        ).pack(anchor="w", pady=(12, 10))
        ttk.Label(
            panel, textvariable=self.message, font=("Microsoft YaHei UI", 10), wraplength=470,
        ).pack(anchor="w", fill="x", pady=8)
        self.progress = ttk.Progressbar(panel, mode="indeterminate")
        self.actions = ttk.Frame(panel)
        self.actions.pack(side="bottom", fill="x")
        self.primary = ttk.Button(self.actions)
        self.primary.pack(side="left")
        self.secondary = ttk.Button(self.actions, text="关闭", command=self.close)
        self.secondary.pack(side="right")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.drain)
        if launch:
            try:
                server, token = parse_launch_url(launch)
                self.client = HelperClient(server)
                self.primary.configure(text="正在连接…", state="disabled")
                self.message.set("正在核验一次性授权凭证…")
                self.run(lambda: self.client.post("claim", token), "claim")
            except (HelperError, ValueError):
                self.show_error("授权链接无效或网站不受信任，请从网站重新打开。")
        else:
            self.description.set("Windows 版 · " + VERSION + " · 无需管理员权限")
            self.message.set("安装后可从网站唤起专用抖音登录窗口。")
            self.primary.configure(text="安装 / 修复助手", command=self.install)
            ttk.Button(self.actions, text="解除关联", command=self.uninstall).pack(side="left", padx=8)

    def run(self, work, kind: str):
        self.busy = True
        self.progress.pack(fill="x", pady=(6, 20), before=self.actions)
        self.progress.start(12)

        def worker():
            try:
                result = work()
                if kind == "claim" and self.cancelled.is_set():
                    self.client.post("fail", result["upload_token"])
                    raise Cancelled("已取消登录。")
                self.events.put((kind, result))
            except Exception as exc:
                message = str(exc) if isinstance(exc, HelperError) else "本地授权发生错误，请关闭助手后重试。"
                self.events.put(("error", message))
            finally:
                self.events.put(("idle", None))

        threading.Thread(target=worker, daemon=False).start()

    def drain(self):
        try:
            while True:
                kind, result = self.events.get_nowait()
                if kind == "message":
                    if not self.closed:
                        self.message.set(result)
                elif kind == "idle":
                    self.busy = False
                    self.progress.stop()
                    self.progress.pack_forget()
                    if self.closed:
                        self.finish_close()
                        return
                elif kind == "error":
                    if not self.closed:
                        self.show_error(result)
                elif kind == "claim":
                    self.claim = result
                    if self.closed:
                        continue
                    if result.get("server") != self.client.server:
                        self.show_error("网站身份不一致，已拒绝读取登录态。")
                        continue
                    self.title.set("确认本次授权账号")
                    host = urlsplit(self.client.server).netloc
                    self.description.set("网站：" + host + "\n保存到账号：" + str(result["username"]))
                    self.message.set("只有此专用窗口的抖音授权会保存到上面的账号。")
                    self.primary.configure(text="打开抖音登录窗口", state="normal", command=self.authorize)
                    self.root.update_idletasks()
                    self.root.geometry("540x" + str(max(300, self.panel.winfo_reqheight() + 40)))
                elif kind == "saved":
                    self.claim = None
                    if not self.closed:
                        self.title.set("抖音授权已保存")
                        detail = (
                            "临时浏览器清理遇到异常，请关闭助手后检查本地助手目录。"
                            if result.get("cleanup_warning") else "可以返回网站继续使用。"
                        )
                        self.message.set("已保存到账号：" + str(result["username"]) + "\n" + detail)
                        self.primary.configure(text="返回网站", state="normal", command=self.return_to_site)
                        self.root.update_idletasks()
                        self.root.geometry("540x" + str(max(300, self.panel.winfo_reqheight() + 40)))
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def authorize(self):
        if self.busy or not self.claim:
            return
        self.primary.configure(state="disabled", text="等待登录…")
        self.run(
            lambda: capture_and_save(
                self.client, self.claim, self.cancelled,
                lambda text: self.events.put(("message", text)),
            ), "saved",
        )

    def return_to_site(self):
        webbrowser.open(self.client.server + "/#cookies")
        self.close()

    def show_error(self, message: str):
        self.title.set("本次授权未完成")
        self.message.set(message)
        self.primary.configure(text="返回网站重新授权", state="normal", command=self.return_to_site if self.client else self.close)

    def install(self):
        try:
            install()
            self.message.set("安装完成。返回网站，点击“打开抖音登录窗口”即可。")
            self.primary.configure(text="打开网站", command=lambda: webbrowser.open(SITE + "/#cookies"))
        except (HelperError, OSError):
            self.message.set("安装失败。请关闭其他助手窗口后重试，或检查系统安全软件提示。")

    def uninstall(self):
        try:
            uninstall()
            self.message.set("已解除网站唤起关联，未修改浏览器或网站账号。")
        except (HelperError, OSError):
            self.message.set("未能解除关联，请关闭其他版本的助手后重试。")

    def close(self):
        if self.closed:
            return
        self.cancelled.set()
        self.closed = True
        self.primary.configure(state="disabled")
        self.secondary.configure(state="disabled")
        self.message.set("正在关闭专用登录窗口…")
        if self.busy:
            return
        self.finish_close()

    def finish_close(self):
        if self.claim:
            claim, self.claim = self.claim, None
            self.run(lambda: self.client.post("fail", claim["upload_token"]), "cancelled")
            self.root.after(100, self.drain)
        else:
            self.root.destroy()


def main():
    if sys.platform != "win32":
        raise SystemExit("The login helper currently supports Windows only.")
    if sys.argv[1:] == ["--self-test"]:
        value = "videoinsight-login://authorize?server=https%3A%2F%2F206.187.222.21&token=" + "x" * 43
        assert parse_launch_url(value) == (SITE, "x" * 43)
        return
    launch = sys.argv[1] if len(sys.argv) == 2 else None
    if len(sys.argv) > 2 or (launch and not launch.startswith("videoinsight-login:")):
        raise SystemExit(2)
    window = HelperWindow(launch)
    window.root.mainloop()


if __name__ == "__main__":
    main()
