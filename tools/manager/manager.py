"""Serve the local Steno Squid Manager and reuse the CLI’s HID console."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from queue import Empty, Queue
import re
import secrets
import signal
import sys
from threading import Lock, Thread, current_thread, main_thread
import time
import webbrowser

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS / "storage"))
from native_install import FACTORY_CAPABILITIES, UNPROVISIONED, prepare_install_image, provision, wait_ready
from build_native_image import FACTORY_KEYS
from native_manage import checked, import_entries
from native_update import backup_all, export_dictionary, overlay_usage, transfer
from import_personal import bluetooth_console_devices, open_bluetooth_console, open_console, parse_named, usb_console_devices
from compile_dict import parse_outline
from native_base_update import factory_package
from native_snapshot import change_snapshot, clear_personal
from native_import_plan import named_import
from native_layout import read_images, read_layout
from manager_repack import capture_approved_repack, install_repack, last_repack_backup, preview_repack, restore_original

REPACK_ACTIONS = {"preview_repack", "apply_repack", "restore_repack_backup"}
IMPORT_ACTIONS = {"preview_import", "apply_import", "import"}
USB_ONLY_ACTIONS = REPACK_ACTIONS | IMPORT_ACTIONS
MAX_REQUEST = 48 * 1024 * 1024
LARGE_JOURNAL_RECORDS = 4096
FACTORY_CHOICES = {key: row[1] for key, row in FACTORY_KEYS.items()}
FACTORY_ACTIONS = {str(row[0]): key for key, row in FACTORY_KEYS.items()}
MAP_REFRESH_ACTIONS = {"refresh", "layout_map", "setup", "import", "apply_import", "compact", "apply_package",
                       "apply_optimize", "clear_personal", "delete", "set", "remove", "reset", "apply_repack",
                       "restore_repack_backup"}
INSTALLABLE_STATUS = re.compile(r"NATIVE2 (?:READY [0-9]+|UNPROVISIONED [0-9]+(?: .*)?)")


def installer_view(console, status, *, reprovision=False, transport="usb"):
    if not INSTALLABLE_STATUS.fullmatch(status):
        raise RuntimeError("The dictionary installer is unavailable. Keep the left half connected and try again.")
    if console.command(FACTORY_CAPABILITIES) != "OK FACTORY2":
        raise RuntimeError("This keyboard did not offer dictionary installation.")
    return {"transport": transport, "setup": {"serial": console.serial, "packages": FACTORY_CHOICES,
                      "expected_status": status, "reprovision": reprovision}}


def read_catalog(console):
    value = json.loads(console.command("native_catalog"))
    if not isinstance(value, list) or not value:
        raise RuntimeError("Provision a keyboard with the matching Steno Squid firmware first.")
    return value


def image_details(console):
    try:
        return read_images(console)
    except (RuntimeError, OSError, ValueError):
        return None


def scan_layout(console, progress):
    for attempt in range(3):
        try:
            return read_layout(console)
        except RuntimeError as error:
            if not str(error).startswith("ERR Layout scan deferred") or attempt == 2:
                raise
            progress("Waiting for flash, then retrying the scan…")
            if str(error) == "ERR Layout scan deferred until the flash and input are idle":
                # Earlier firmware wakes flash for status queries but not for the map.
                console.command("native_install status", timeout=3)
            time.sleep(0.25)


def load_layout(console, progress):
    layout = scan_layout(console, progress)
    layout["images"] = image_details(console)
    return layout


def load_view(console, action, progress, show_view=None):
    state = read_state(console)
    result = {"state": state}
    if state.get("layout_map") and action in MAP_REFRESH_ACTIONS:
        try:
            progress("Scanning memory…")
            result["layout_map"] = load_layout(console, progress)
            result["layout_map"]["generation"] = state["generation"]
        except (RuntimeError, OSError, ValueError) as error:
            result["layout_map_error"] = str(error)
    if state.get("catalog") and action in MAP_REFRESH_ACTIONS:
        if show_view is not None:
            show_view(dict(result))
        try:
            progress("Measuring journal sizes…")
            usage = overlay_usage(console)
            if "layout_map" in result:
                expected = f"NATIVE2 READY {state['generation']}"
                if console.command("native_install status") == expected:
                    result["layout_map"]["journal_usage"] = usage
            result["usage"] = usage
            result["journal_budget_exceeded"] = (
                sum(row["count"] for row in usage.values()) >= LARGE_JOURNAL_RECORDS
            )
        except (RuntimeError, OSError, ValueError) as error:
            result["usage_error"] = str(error)
    return result


def read_state(console):
    transport = getattr(console, "transport", "usb")
    install_status = console.command("native_install status")
    if install_status == "ERR Native flash busy":
        raise RuntimeError("The keyboard is still checking dictionary flash. Try connecting again in a moment.")
    if install_status.startswith("NATIVE2 UNPROVISIONED "):
        backup = last_repack_backup(console.serial) if transport == "usb" else None
        if backup and console.command("native_install update recovery-capabilities") == "OK RECOVERY1":
            return {"transport": transport, "setup": {"serial": console.serial, "repack_backup": backup.name}}
        if install_status == UNPROVISIONED:
            return installer_view(console, install_status, transport=transport)
        raise RuntimeError("The keyboard could not open its dictionary store. Use USB recovery or explicit reprovisioning.")
    if install_status.startswith("NATIVE2 INSTALL "):
        raise RuntimeError("A dictionary install is in progress. Keep the keyboard connected and refresh when it finishes.")
    if not install_status.startswith("NATIVE2 READY "):
        raise RuntimeError("Connect the left half running Steno Squid Native firmware.")
    try:
        status = json.loads(console.command("user_dictionary_status"))
    except json.JSONDecodeError as error:
        raise RuntimeError("The dictionary store did not respond. Refresh the connection and try again.") from error
    if not isinstance(status, dict) or status.get("format") != "SQBCAT02" or not status.get("ready"):
        raise RuntimeError("The dictionary store is unavailable. Check the connection and firmware.")
    layouts = json.loads(console.command("get_parameter available_host_layouts"))
    adaptive = console.command("native_install update snapshot-capabilities") == "OK SNAPSHOT2"
    catalog = read_catalog(console)
    factory = console.command(FACTORY_CAPABILITIES) == "OK FACTORY2"
    hints = console.command("stroke_hints")
    return {
        "serial": console.serial,
        "transport": transport,
        "catalog": catalog,
        "layouts": layouts,
        "layout": console.command("get_parameter host_layout"),
        "hints": hints if hints in {"auto", "off"} else None,
        "read_only": status["read_only"],
        "auto_checkpoints": status.get("auto_checkpoints", 0),
        "auto_reclaimed": status.get("auto_reclaimed", 0),
        "tail": status["tail"],
        "generation": status.get("generation"),
        "maintenance_active": bool(status.get("maintenance")),
        "personal_conversion": adaptive and console.command(
            "native_install update personal-snapshot-capabilities") == "OK PERSONAL1",
        "journal_conversion": adaptive and console.command(
            "native_install update journal-snapshot-capabilities") == "OK JOURNAL1",
        "personal_clear": adaptive and console.command(
            "native_install update snapshot-clear-capabilities") == "OK CLEAR1",
        "adaptive_imports": adaptive,
        "factory_actions": FACTORY_ACTIONS if factory else {},
        "repack": transport == "usb" and adaptive and not status["read_only"] and console.command(
            "native_install update repack-capabilities") == "OK REPACK1",
        "layout_map": adaptive and console.command("native_install update layout-capabilities") == "OK MAP1",
    }


def safe_name(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\n\r\0"):
        raise ValueError("Invalid dictionary name")
    return value


def read_import(source):
    try:
        entries, names = parse_named(source)
    except json.JSONDecodeError as error:
        raise ValueError(f"The dictionary file is not valid JSON (line {error.lineno}, column {error.colno}).") from error
    if not entries:
        raise ValueError("This JSON dictionary contains zero entries. Choose a file with outline-to-translation entries.")
    return entries, names


def setup_keyboard(serial, request, progress, show_view=None, *, transport="usb"):
    opener = open_bluetooth_console if transport == "bluetooth" else open_console
    ready_options = {"opener": opener} if transport == "bluetooth" else {}
    if request.get("approved") is not True:
        raise ValueError("Confirm that setup can clear the external dictionary pool.")
    target = request.get("serial")
    if not isinstance(target, str) or not target or (serial and serial != target):
        raise ValueError("Reconnect the keyboard and confirm setup for its serial number.")
    packages = request.get("packages")
    expected_status = request.get("expected_status", UNPROVISIONED)
    if not isinstance(expected_status, str) or not INSTALLABLE_STATUS.fullmatch(expected_status):
        raise ValueError("Open the installer to verify the keyboard before proceeding.")
    if expected_status != UNPROVISIONED and request.get("confirm_serial") != target:
        raise ValueError("Confirm the keyboard serial before replacing the dictionary pool.")
    with opener(target) as console:
        if console.serial != target or console.command("native_install status") != expected_status:
            raise RuntimeError("The selected keyboard’s dictionary status changed. Reopen the installer.")
        if console.command(FACTORY_CAPABILITIES) != "OK FACTORY2":
            raise RuntimeError("This keyboard did not offer dictionary installation.")
        if (not isinstance(packages, list) or not packages
                or any(not isinstance(package, str) or package not in FACTORY_CHOICES for package in packages)
                or len(set(packages)) != len(packages)):
            raise ValueError("Select at least one available factory dictionary.")
        progress("Preparing the selected factory dictionaries…")
        image, manifest = prepare_install_image(packages=packages)
        provision(console, image, manifest, progress, expected_status=expected_status)
    progress("Waiting for the keyboard to reconnect…")
    try:
        view = wait_ready(target, load_state=lambda console: load_view(console, "setup", progress, show_view),
                          **ready_options)
    except RuntimeError:
        return {"commit_pending": True}
    return {**view, "installed": True}


def run_job(serial, request, progress, show_view=None, *, transport="usb"):
    action = request["action"]
    if transport == "bluetooth" and action in USB_ONLY_ACTIONS:
        if action in IMPORT_ACTIONS:
            raise ValueError("Connect over USB to import JSON.")
        raise ValueError("Connect over USB to repack or restore a repack backup.")
    opener = open_bluetooth_console if transport == "bluetooth" else open_console
    ready_options = {"opener": opener} if transport == "bluetooth" else {}
    if action == "setup":
        return setup_keyboard(serial, request, progress, show_view, transport=transport)
    if action in {"apply_repack", "restore_repack_backup"}:
        restore = action == "restore_repack_backup"
        with opener(serial) as console:
            serial = console.serial
            if not restore:
                source, plan = capture_approved_repack(console, request, progress)
        if restore:
            restore_original(serial, request, progress)
        else:
            install_repack(source, plan, progress)
        with opener(serial) as console:
            return {**load_view(console, action, progress, show_view), "repacked": not restore, "restored": restore}
    published = False
    report = {}
    with opener(serial) as console:
        serial = console.serial
        if action == "open_installer":
            return {"state": installer_view(console, console.command("native_install status", timeout=5),
                                            reprovision=True, transport=transport)}
        if action in {"refresh", "layout_map"}:
            return load_view(console, action, progress, show_view)
        if action == "preview_repack":
            return {"repack_plan": preview_repack(console, progress)}
        if action == "preview_compact":
            layout = None
            if console.command("native_install update layout-capabilities") == "OK MAP1":
                try:
                    generation = json.loads(console.command("user_dictionary_status"))["generation"]
                    layout = read_layout(console)
                    layout["images"] = image_details(console)
                except RuntimeError:
                    pass
            cleanup = transfer(console, progress=progress, preview_compact=True)
            if layout and generation != json.loads(console.command("user_dictionary_status"))["generation"]:
                layout = None
            if layout:
                layout["generation"] = generation
            return {"cleanup": cleanup, "layout_map": layout}
        catalog = read_catalog(console)
        by_id = {row["id"]: row for row in catalog}
        selected = by_id.get(request.get("id"))
        if action in {"enable", "move", "rename", "delete", "export", "set", "remove", "reset", "preview_optimize", "apply_optimize"} and selected is None:
            raise ValueError("Select a dictionary from the current catalog.")
        name = safe_name(selected["name"]) if selected else None
        if selected and selected["kind"] == "uninstalled" and action in {"enable", "set", "remove", "reset", "export"}:
            raise ValueError("Install the factory dictionary before using it.")
        if action in {"preview_package", "apply_package"}:
            applying = action == "apply_package"
            token = request.get("token")
            restore = request.get("restore")
            if not isinstance(restore, bool) or (applying and (not isinstance(token, str) or len(token) != 64)):
                raise ValueError("Preview the factory dictionary before applying changes.")
            progress("Checking factory dictionary and flash space…")
            outcome = factory_package(console, request.get("package"), restore=restore,
                                      confirmation=token if applying else None, progress=progress)
            if not applying:
                return {"package_plan": outcome}
            report = {"package_changed": outcome}
            published = True
        elif action == "enable":
            if not isinstance(request["enabled"], bool):
                raise ValueError("Expected an enabled checkbox value")
            verb = "enable" if request["enabled"] else "disable"
            checked(console, f"{verb}_dictionary {name}")
        elif action == "hints":
            mode = request.get("mode")
            if mode not in ("auto", "off"):
                raise ValueError("Choose Auto or Off for shorter-outline hints.")
            checked(console, f"stroke_hints {mode}")
        elif action == "layout":
            layout = request["layout"]
            if layout not in json.loads(console.command("get_parameter available_host_layouts")):
                raise ValueError("Select a supported host layout.")
            checked(console, "set_host_layout " + layout)
        elif action in {"set", "remove", "reset"}:
            outline = request["outline"]
            if not isinstance(outline, str) or not 1 <= len(parse_outline(outline)) <= 16:
                raise ValueError("Enter an outline of up to 16 strokes.")
            if action == "reset" and selected["kind"] != "base":
                raise ValueError("Revert to original is available for factory dictionaries.")
            if action == "set":
                entries, names = parse_named(json.dumps({outline: request["translation"]}))
                import_entries(console, selected["id"], entries, names, progress)
            else:
                verb = "remove" if action == "remove" else "reset"
                checked(console, f"native_{verb}_entry {selected['id']} {outline}")
        elif action == "export":
            progress(f"Exporting {name}…")
            entries = export_dictionary(console, name)
            return {"download": entries, "filename": name + ".json", "count": len(entries)}
        elif action == "backup":
            progress("Reading saved edits, dictionary order and checkboxes…")
            return {"download": backup_all(console), "filename": "stenosquid-backup.json"}
        elif action == "clear_personal":
            if request.get("confirm") != "CLEAR" or not selected or name != "user_dictionary":
                raise ValueError("Select your personal dictionary and type CLEAR to confirm.")
            if console.command("native_install update snapshot-clear-capabilities") != "OK CLEAR1":
                raise RuntimeError("Flash the current left firmware before clearing the personal dictionary.")
            progress("Clearing personal entries…")
            published = clear_personal(console, selected["id"], progress)
            report = {"cleared": True}
        elif action in {"preview_optimize", "apply_optimize"}:
            applying = action == "apply_optimize"
            expected = request.get("token")
            if applying and (not isinstance(expected, str) or len(expected) != 64):
                raise ValueError("Preview the optimization before staging.")
            operation = request.get("operation")
            if (selected["kind"], operation) not in {
                ("personal", "personal"), ("named", "convert"), ("snapshot", "consolidate")
            }:
                raise ValueError("Choose an eligible journal or user snapshot.")
            progress(f"Reading {name} and calculating staging space…")
            outcome = change_snapshot(console, operation, name, stage=applying,
                                      preview=not applying, expected=expected if applying else None,
                                      target_id=selected["id"], progress=progress)
            if not applying:
                return {"optimization_plan": outcome}
            published = outcome
            report = {"optimized": name}
        elif action in {"preview_import", "apply_import"}:
            entries, _ = read_import(request["source"])
            staging = action == "apply_import"
            expected = request.get("token")
            if staging and (not isinstance(expected, str) or len(expected) != 64):
                raise ValueError("Preview the import before staging.")
            import_report = named_import(console, safe_name(request["name"]), entries,
                                         stage=staging,
                                         expected=expected, progress=progress)
            if not staging:
                return {"import_plan": import_report}
            published = True
            report = {"imported": len(entries)}
        elif action in {"move", "rename", "delete", "import", "compact"}:
            if action in {"rename", "delete"} and (name == "user_dictionary" or selected["kind"] not in {"named", "snapshot"}):
                raise ValueError("Rename and delete are available for added dictionaries.")
            if console.command("native_install update manage-capabilities") != "OK MANAGE1":
                raise RuntimeError("Flash the matching Manager firmware before writing.")
            options = {}
            entries = None
            if action == "compact":
                token = request.get("token")
                if not isinstance(token, str) or len(token) != 64:
                    raise ValueError("Preview journal cleanup before compacting.")
                options["expected_compact"] = token
            elif action == "move":
                options["move"] = (name, int(request["position"]))
            elif action == "rename":
                options["rename"] = (name, safe_name(request["name"]))
            elif action == "delete":
                if request.get("confirm") != name:
                    raise ValueError("Confirm the name of the dictionary to delete.")
                options["delete"] = name
            elif action == "import":
                entries, _ = read_import(request["source"])
                if request.get("name"):
                    raise ValueError("Preview named imports before saving the compact dictionary.")
            if action == "delete" and selected["kind"] == "snapshot":
                published = change_snapshot(console, "delete", name, stage=True, progress=progress)
            else:
                published = transfer(console, entries, progress, **options)
            if action == "import":
                report = {"imported": len(entries)}
        else:
            raise ValueError("Unknown Manager operation")

    if published:
        progress("Keyboard acknowledged the catalog commit. Waiting for it to reconnect…")
        try:
            view = wait_ready(serial, load_state=lambda console: load_view(console, action, progress, show_view),
                              checking=lambda: progress("Saved. Keyboard is checking dictionaries…"), **ready_options)
        except RuntimeError:
            return {"commit_pending": True}
        return {**view, **report}
    with opener(serial) as console:
        return {**load_view(console, action, progress, show_view), **report}


class Jobs:
    def __init__(self, serial):
        self.serial = serial
        self.lock = Lock()
        self.pending = Queue()
        self.stopping = False
        self.connected_serial = None
        self.transport = "usb"
        self.next_connection_check = 0
        self.current = {"id": "", "status": "idle"}

    def start(self, request):
        with self.lock:
            if self.stopping:
                raise ValueError("Manager is shutting down.")
            if self.current["status"] == "running":
                raise ValueError("A keyboard operation is already running.")
            identity = secrets.token_hex(8)
            self.current = {"id": identity, "status": "running", "message": "Opening keyboard connection…"}
            self.pending.put_nowait(request)
        return identity

    def update(self, **values):
        with self.lock:
            self.current.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.current)

    def serve(self):
        # macOS HIDAPI enumerates devices through the current CoreFoundation
        # run loop. Keep every HID session on the process main thread.
        while not self.stopping:
            try:
                request = self.pending.get(timeout=0.2)
            except Empty:
                self.check_connection()
                continue
            self.run(request)

    def run(self, request):
        action = request["action"]
        if action == "disconnect":
            self.connected_serial = None
            self.update(status="done", result={"disconnected": True}, view=None, connection_lost=False)
            return
        serial = self.connected_serial or self.serial
        transport = request.get("transport", self.transport) if action == "refresh" else self.transport
        try:
            if transport == "auto" and action == "refresh":
                if usb_console_devices(serial):
                    transport = "usb"
                elif bluetooth_console_devices():
                    transport = "bluetooth"
                elif request.get("initial") is True:
                    self.connected_serial = None
                    self.update(status="done", result={"disconnected": True}, view=None, connection_lost=False)
                    return
                else:
                    raise ValueError("Connect the left half by USB or pair it in your computer’s Bluetooth settings.")
            if transport not in ("usb", "bluetooth"):
                raise ValueError("Unknown keyboard connection.")
            result = run_job(serial, request, lambda message: self.update(message=message),
                             lambda view: self.update(view=view), transport=transport)
        except (Exception, SystemExit) as error:
            if action == "refresh":
                if transport in ("usb", "bluetooth"):
                    self.transport = transport
                self.connected_serial = None
            self.update(status="error", message=str(error), view=None)
        else:
            state = result.get("state")
            if state:
                self.transport = transport
                self.connected_serial = state.get("setup", state)["serial"]
            if result.get("commit_pending"):
                self.connected_serial = None
            self.next_connection_check = time.monotonic() + 3
            self.update(status="done", result=result, view=None, connection_lost=bool(result.get("commit_pending")))

    def check_connection(self):
        now = time.monotonic()
        if not self.connected_serial or now < self.next_connection_check:
            return
        self.next_connection_check = now + 3
        try:
            present = bool(bluetooth_console_devices() if self.transport == "bluetooth"
                           else usb_console_devices(self.connected_serial))
        except (OSError, RuntimeError):
            present = False
        if not present:
            self.connected_serial = None
            self.update(connection_lost=True)


def serve(serial=None):
    token = secrets.token_urlsafe()
    jobs = Jobs(serial)
    assets = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/manager.css": ("manager.css", "text/css; charset=utf-8"),
        "/manager.js": ("manager.js", "text/javascript; charset=utf-8"),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, data, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def json(self, status, value):
            self.send(status, json.dumps(value, ensure_ascii=True).encode(), "application/json")

        def allowed(self):
            if self.headers.get("Host") != host:
                self.json(403, {"error": "Unexpected host"})
                return False
            return True

        def authorized(self):
            if not self.allowed():
                return False
            if self.headers.get("X-StenoSquid-Token") != token:
                self.json(403, {"error": "Open the Manager from its local address."})
                return False
            return True

        def do_GET(self):
            if self.path == "/api/job":
                if self.authorized():
                    self.json(200, jobs.snapshot())
            elif self.allowed():
                if self.path not in assets:
                    self.send_error(404)
                    return
                name, kind = assets[self.path]
                data = (HERE / name).read_bytes()
                self.send(200, data.replace(b"MANAGER_SESSION_TOKEN", token.encode()) if self.path == "/" else data, kind)

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/api/job" or self.headers.get("Origin") != url or self.headers.get_content_type() != "application/json":
                self.json(403, {"error": "Invalid Manager request"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST:
                    raise ValueError("The upload is empty or exceeds 48 MiB.")
                self.connection.settimeout(30)
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict) or not isinstance(request.get("action"), str):
                    raise ValueError("Invalid keyboard operation")
                identity = jobs.start(request)
            except (OSError, ValueError) as error:
                self.json(400, {"error": str(error)})
                return
            self.json(202, {"id": identity})

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        host = f"127.0.0.1:{server.server_port}"
        url = "http://" + host

        def stop(*_):
            jobs.stopping = True

        def serve_http():
            try:
                server.serve_forever()
            finally:
                stop()

        # Finish a pending flash transaction before exiting on Ctrl-C.
        previous = signal.signal(signal.SIGINT, stop) if current_thread() is main_thread() else None
        http = Thread(target=serve_http)
        http.start()
        try:
            print(f"Steno Squid Manager is running at {url}", flush=True)
            webbrowser.open(url)
            jobs.serve()
        finally:
            server.shutdown()
            http.join()
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
