"""Browser adapters only; dictionary operations remain in manager.py."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sys

from js import browser_rpc, browser_sleep
from pyodide.ffi import run_sync

sys.path[:0] = ["/app/tools/manager", "/app/tools/storage", "/app/tools/dictionaries", "/app/tools/dict_compiler"]


def rpc(operation, **values):
    result = json.loads(run_sync(browser_rpc(json.dumps(dict(operation=operation, **values)))))
    if "error" in result:
        raise RuntimeError(result["error"])
    return result.get("value")


import import_personal
import time
time.sleep = lambda seconds: run_sync(browser_sleep(seconds))


class BrowserDevice:
    def write(self, data):
        rpc("write", data=list(data[1:]))  # WebHID takes report ID separately.
        return len(data)

    def read(self, size, timeout):
        return rpc("read", timeout=timeout)


@contextmanager
def open_console(serial=None):
    rpc("open")
    console = import_personal.Console(BrowserDevice())
    identity = console.command("native_install identity", timeout=3)
    match = re.fullmatch(r"OK SQMANAGER1 ([0-9A-F]+)", identity)
    if not match or (serial and serial != match[1]):
        raise RuntimeError("The selected keyboard identity differs. Connect the intended Steno Squid left half.")
    console.serial = match[1]
    connection = console.command("native_install connection", timeout=3)
    if connection not in {"OK USB", "OK BLUETOOTH"}:
        raise RuntimeError("Update the left firmware from the matching browser Manager release to identify its connection safely.")
    console.transport = "usb" if connection == "OK USB" else "bluetooth"
    yield console


# Patch before importing the shared backend, so all internal reconnects use WebHID.
import_personal.open_console = open_console
import_personal.open_bluetooth_console = open_console

import native_recovery


def save_atomic(path, document, dump=json.dump):
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
    # Wait for the user-selected local backup file to close BEFORE returning
    # to shared code that may erase flash. Disk errors stop the operation here.
    rpc("persist", path=str(path), data=raw)
    path.write_text(raw, encoding="utf-8")


native_recovery.save_atomic = save_atomic
native_recovery.sync_directory = lambda path: None  # The browser closes each local file before acknowledging it.

import native_repack


def read_bundle(path, *, durable=False):
    path = path.resolve(strict=True)
    raw = path.read_bytes()
    if durable and rpc("load", path=str(path)) != raw.decode("utf-8"):
        raise RuntimeError("The persisted recovery file differs. No flash erase was started.")
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


native_repack.read_bundle = read_bundle

import manager


connected_serial = None


def _run_request(source):
    global connected_serial
    request = json.loads(source)
    if request["action"] == "disconnect":
        rpc("close")
        connected_serial = None
        return json.dumps({"disconnected": True})
    expected = None if request["action"] in {"refresh", "open_installer"} else connected_serial
    if expected is None and request["action"] not in {"refresh", "open_installer"}:
        raise RuntimeError("Connect the keyboard before making changes.")
    with open_console(expected) as console:
        serial, transport = console.serial, console.transport
    connected_serial = serial
    result = manager.run_job(serial, request, lambda message: rpc("progress", message=message),
                             lambda view: rpc("view", view=view), transport=transport)
    return json.dumps(result)


def probe_connection():
    """Read identity and transport without changing keyboard settings."""
    try:
        with open_console() as console:
            return json.dumps({"serial": console.serial, "transport": console.transport})
    except Exception as error:
        return json.dumps({"error": str(error)})


def run_request(source):
    try:
        return _run_request(source)
    except Exception as error:
        return json.dumps({"_browser_error": str(error)})
