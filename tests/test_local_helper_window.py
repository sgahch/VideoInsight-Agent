"""Exercise the native UI's event coordination without opening real windows."""
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import local_helper.main as native


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def configure(self, **kwargs):
        pass

    def stop(self):
        pass

    def pack_forget(self):
        pass


def window():
    view = native.HelperWindow.__new__(native.HelperWindow)
    view.events = queue.Queue()
    view.cancelled = threading.Event()
    view.busy = True
    view.closed = False
    view.claim = None
    view.primary = Widget()
    view.secondary = Widget()
    view.progress = Widget()
    view.title = Value()
    view.message = Value()
    view.description = Value()
    state = {"destroyed": False, "scheduled": []}
    view.root = SimpleNamespace(
        destroy=lambda: state.update(destroyed=True),
        after=lambda *args: state["scheduled"].append(args),
        update_idletasks=lambda: None,
        geometry=lambda *args: None,
    )
    view.panel = SimpleNamespace(winfo_reqheight=lambda: 260)
    calls = []
    view.client = SimpleNamespace(
        server="https://206.187.222.21",
        post=lambda *args: calls.append(args),
    )
    work = []
    view.run = lambda callback, kind: work.append((callback, kind))
    return view, state, calls, work


def test_close_revokes_claim_already_queued_before_ui_consumes_it():
    view, state, calls, work = window()
    claim = {"upload_token": "synthetic-upload-token", "server": view.client.server,
             "username": "synthetic@example.invalid"}
    view.events.put(("claim", claim))
    view.events.put(("idle", None))
    view.close()
    view.drain()
    assert not state["destroyed"], "Wait for the cancellation acknowledgment before exiting"
    assert len(work) == 1 and work[0][1] == "cancelled"
    work[0][0]()
    assert calls == [("fail", claim["upload_token"])]
    view.events.put(("idle", None))
    view.drain()
    assert state["destroyed"]


def test_close_without_queued_claim_does_not_resurrect_ui():
    view, state, calls, work = window()
    view.events.put(("error", "synthetic cancelled request"))
    view.events.put(("idle", None))
    view.close()
    view.drain()
    assert state["destroyed"] and not work and not calls


def test_close_after_acknowledged_save_does_not_revoke_success():
    view, state, calls, work = window()
    view.claim = {"upload_token": "synthetic-upload-token"}
    view.events.put(("saved", {"username": "synthetic@example.invalid"}))
    view.events.put(("idle", None))
    view.close()
    view.drain()
    assert state["destroyed"] and not work and not calls
