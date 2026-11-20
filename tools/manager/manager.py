"""Serve the local Steno Squid Manager and reuse the CLI's USB HID transport."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from queue import Empty, Queue
import secrets
import signal
import sys
from threading import Lock, Thread, current_thread, main_thread
import webbrowser

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS / "storage"))
from native_install import wait_ready
from native_manage import checked, import_entries
from native_update import backup_all, export_dictionary, overlay_usage, transfer
from import_personal import open_console, parse_named
from compile_dict import parse_outline
from native_base_update import changed_pins, replace_pinned
from native_restore import restore_all
from native_snapshot import change_snapshot, clear_personal
from native_import_plan import named_import
from native_layout import read_images, read_layout

MAX_REQUEST = 48 * 1024 * 1024


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


def read_state(console):
    status = json.loads(console.command("user_dictionary_status"))
    if status.get("format") != "SQBCAT02" or not status.get("ready"):
        raise RuntimeError("The dictionary store is unavailable. Check USB and firmware.")
    layouts = json.loads(console.command("get_parameter available_host_layouts"))
    adaptive = console.command("native_install update snapshot-capabilities") == "OK SNAPSHOT2"
    catalog = read_catalog(console)
    return {
        "catalog": catalog,
        "images": image_details(console) if any(row.get("image_bytes") for row in catalog) else [],
        "layouts": layouts,
        "layout": console.command("get_parameter host_layout"),
        "read_only": status["read_only"],
        "auto_checkpoints": status.get("auto_checkpoints", 0),
        "auto_reclaimed": status.get("auto_reclaimed", 0),
        "tail": status["tail"],
        "generation": status.get("generation"),
        "maintenance_active": bool(status.get("maintenance")),
        "personal_conversion": adaptive and console.command(
            "native_install update personal-snapshot-capabilities") == "OK PERSONAL1",
        "personal_clear": adaptive and console.command(
            "native_install update snapshot-clear-capabilities") == "OK CLEAR1",
        "adaptive_imports": adaptive,
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


def run_job(serial, request, progress):
    action = request["action"]
    if action == "restore":
        if request.get("confirm") != "RESTORE":
            raise ValueError("Type RESTORE to confirm replacing this keyboard’s dictionaries.")
        serial = restore_all(serial, json.loads(request["source"]), progress)
        with open_console(serial) as console:
            return {"state": read_state(console), "restored": True}
    published = False
    report = {}
    with open_console(serial) as console:
        serial = console.serial
        if action == "refresh":
            return {"state": read_state(console)}
        if action == "layout_map":
            layout = read_layout(console)
            layout["images"] = image_details(console)
            return {"layout_map": layout}
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
            return {"cleanup": cleanup, "layout_map": layout}
        catalog = read_catalog(console)
        by_id = {row["id"]: row for row in catalog}
        selected = by_id.get(request.get("id"))
        if action in {"enable", "move", "rename", "delete", "export", "set", "remove", "reset", "preview_optimize", "apply_optimize"} and selected is None:
            raise ValueError("Select a dictionary from the current catalog.")
        name = safe_name(selected["name"]) if selected else None
        if action == "enable":
            if not isinstance(request["enabled"], bool):
                raise ValueError("Expected an enabled checkbox value")
            verb = "enable" if request["enabled"] else "disable"
            checked(console, f"{verb}_dictionary {name}")
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
        elif action in {"backup", "measure"}:
            if action == "backup":
                progress("Reading saved edits, dictionary order and checkboxes…")
                complete = request.get("complete") is True
                return {"download": backup_all(console, complete=complete),
                        "filename": "stenosquid-rescue.json" if complete else "stenosquid-backup.json"}
            progress("Counting saved edits across dictionaries…")
            return {"state": read_state(console), "usage": overlay_usage(console)}
        elif action == "check_updates":
            return {"updates": changed_pins(catalog)}
        elif action == "apply_updates":
            if request.get("approved") is not True:
                raise ValueError("Confirm the pinned dictionary update.")
            changed, conflicts = replace_pinned(console, catalog, progress)
            published = bool(changed)
            report = {"updated": changed, "conflicts": conflicts}
        elif action == "clear_personal":
            if request.get("confirm") != "CLEAR" or not selected or name != "user_dictionary":
                raise ValueError("Select your personal dictionary and type CLEAR to confirm.")
            progress("Clearing personal entries…")
            if console.command("native_install update snapshot-clear-capabilities") == "OK CLEAR1":
                published = clear_personal(console, selected["id"], progress)
            elif selected["kind"] == "personal":
                checked(console, "reset_user_dictionary")
            else:
                raise RuntimeError("Update the experimental left firmware to clear a compact personal dictionary.")
            report = {"cleared": True}
        elif action in {"preview_optimize", "apply_optimize"}:
            applying = action == "apply_optimize"
            expected = request.get("token")
            if applying and (not isinstance(expected, str) or len(expected) != 64):
                raise ValueError("Preview the optimization before staging.")
            operation = request.get("operation")
            if (selected["kind"], operation) not in {("personal", "personal"), ("snapshot", "consolidate")}:
                raise ValueError("Choose an eligible personal journal or user snapshot.")
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
                                         preference=request.get("preference", "auto"), stage=staging,
                                         expected=expected, progress=progress)
            if not staging:
                return {"import_plan": import_report}
            published = True
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
                label = request.get("name", "")
                if label:
                    options["name"] = safe_name(label)
                    reply = console.command("native_install update named-capabilities").split()
                    if len(reply) != 3 or reply[:2] != ["OK", "NAMED1"]:
                        raise RuntimeError("This firmware cannot create a named dictionary.")
                    options["limit"] = int(reply[2])
            if action == "delete" and selected["kind"] == "snapshot":
                published = change_snapshot(console, "delete", name, stage=True, progress=progress)
            else:
                published = transfer(console, entries, progress, **options)
        else:
            raise ValueError("Unknown Manager operation")

    if published:
        progress("Keyboard acknowledged the catalog commit. Waiting for it to reconnect…")
        try:
            wait_ready(serial)
        except RuntimeError as exc:
            raise RuntimeError("The keyboard acknowledged the commit, but USB did not reconnect in time. "
                               "Refresh and check the catalog before retrying.") from exc
    with open_console(serial) as console:
        state = read_state(console)
    return {"state": state, **report}


class Jobs:
    def __init__(self, serial):
        self.serial = serial
        self.lock = Lock()
        self.pending = Queue()
        self.stopping = False
        self.current = {"id": "", "status": "idle"}

    def start(self, request):
        with self.lock:
            if self.stopping:
                raise ValueError("Manager is shutting down.")
            if self.current["status"] == "running":
                raise ValueError("A keyboard operation is already running.")
            identity = secrets.token_hex(8)
            self.current = {"id": identity, "status": "running", "message": "Connecting over USB HID…"}
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
                continue
            self.run(request)

    def run(self, request):
        try:
            result = run_job(self.serial, request, lambda message: self.update(message=message))
        except (Exception, SystemExit) as error:
            self.update(status="error", message=str(error))
        else:
            self.update(status="done", result=result)


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
