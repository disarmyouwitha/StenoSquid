import {HidConnection} from "./hid.js";

export function unsupportedReason(scope = globalThis) {
  if (!scope.isSecureContext) return "Open the downloaded StenoSquid Manager HTML file in desktop Chrome or Edge.";
  if (!scope.navigator.hid) return "Direct keyboard access needs desktop Chrome or Edge. Safari and Firefox do not support WebHID. The Python Manager also remains available.";
  if (!scope.WebAssembly?.Suspending) return "Update Chrome or Edge to version 137 or newer to run the offline Manager.";
  return "";
}

export class BrowserBackend {
  constructor() {
    this.job = {id: "", status: "idle"};
    this.reason = unsupportedReason();
    this.ready = null;
    this.folder = null;
    this.serial = null;
    this.forceChooser = false;
    this.hid = this.reason ? null : new HidConnection(navigator.hid, () => { this.job.connection_lost = true; });
    globalThis.browser_rpc = async (source) => JSON.stringify(await this.service(JSON.parse(source)));
    globalThis.browser_sleep = (seconds) => new Promise((resolve) => setTimeout(resolve, seconds * 1000));
    addEventListener("beforeunload", (event) => {
      if (this.job.status !== "running") return;
      event.preventDefault(); event.returnValue = "";
    });
    const recovery = document.createElement("button");
    recovery.textContent = "Open recovery folder";
    recovery.type = "button";
    recovery.disabled = Boolean(this.reason);
    recovery.onclick = async () => {
      if (this.job.status === "running") return;
      try {
        const folder = await showDirectoryPicker({mode: "readwrite"});
        await this.initialize();
        this.folder = folder;
        await this.restoreFiles();
        document.querySelector("#status").textContent = "Recovery folder loaded. Select Connect to check the keyboard.";
      } catch (error) { document.querySelector("#status").textContent = error.message; }
    };
    const choose = document.createElement("button");
    choose.textContent = "Choose device…";
    choose.type = "button";
    choose.disabled = Boolean(this.reason);
    choose.title = "Authorize another keyboard connection. Connect prefers USB among authorized connections.";
    choose.onclick = () => {
      const connect = document.querySelector("#refresh");
      if (connect.disabled) return;
      this.forceChooser = true;
      connect.click();
    };
    document.querySelector("#refresh").title = "Prefer USB; use Bluetooth when USB is unavailable";
    document.querySelector("header .toolbar").append(choose, recovery);
  }
  initialize() {
    if (!this.ready) this.ready = this.boot();
    return this.ready;
  }
  async boot() {
    this.python = await loadPyodide({indexURL: "https://offline.invalid/", lockFileContents: offlineAssets.lock});
    this.python.unpackArchive(offlineBytes(offlineAssets.runtime), "zip", {extractDir: "/app"});
    const bridge = this.python.FS.readFile("/app/tools/manager/browser/bridge.py", {encoding: "utf8"});
    this.python.runPython(bridge);
  }
  async connect({devices, explicit}) {
    const candidates = [];
    let chosenSerial = explicit ? null : this.serial;
    let firstError;
    for (const [index, device] of devices.entries()) {
      this.job.message = "Checking keyboard connections; preferring USB…";
      try {
        await this.hid.select([device]);
        const info = JSON.parse(await this.python.runPythonAsync("probe_connection()"));
        if (info.error) throw new Error(info.error);
        if (explicit && index === 0) chosenSerial = info.serial;
        candidates.push({device, ...info});
      } catch (error) {
        firstError ??= error;
        if (explicit && index === 0) throw error;
      }
    }
    const eligible = candidates.filter(candidate => !chosenSerial || candidate.serial === chosenSerial);
    if (new Set(eligible.map(candidate => candidate.serial)).size > 1) {
      throw new Error("More than one keyboard is available. Select Choose device to pick your keyboard.");
    }
    const preferred = eligible.find(candidate => candidate.transport === "usb") ?? eligible[0];
    if (!preferred) throw firstError ?? new Error("The connected keyboard is unavailable. Select Choose device to connect another keyboard.");
    await this.hid.select([preferred.device]);
    this.serial = preferred.serial;
  }
  async file(path, create = false) {
    const match = /^\/home\/pyodide\/\.stenosquid\/repack\/([a-f0-9]{64})\/([a-f0-9]{32})\/(original|optimized|attempt)\.json$/.exec(path);
    if (!match || !this.folder) throw new Error("Choose a recovery folder before repacking or restoring dictionaries.");
    const keyboard = await this.folder.getDirectoryHandle(match[1], {create});
    const plan = await keyboard.getDirectoryHandle(match[2], {create});
    return {plan, name: match[3] + ".json"};
  }
  async restoreFiles() {
    for await (const [identity, keyboard] of this.folder.entries()) {
      if (keyboard.kind !== "directory" || !/^[a-f0-9]{64}$/.test(identity)) continue;
      for await (const [token, plan] of keyboard.entries()) {
        if (plan.kind !== "directory" || !/^[a-f0-9]{32}$/.test(token)) continue;
        for (const name of ["original", "optimized", "attempt"]) {
          try {
            const file = await (await plan.getFileHandle(name + ".json")).getFile();
            if (file.size > 48 * 1024 * 1024) throw new Error("Recovery file exceeds 48 MiB.");
            const directory = `/home/pyodide/.stenosquid/repack/${identity}/${token}`;
            this.python.FS.mkdirTree(directory);
            const target = `${directory}/${name}.json`;
            this.python.FS.writeFile(target, await file.text());
            // Shared recovery selects the latest attempt by modification time.
            this.python.FS.utime(target, file.lastModified / 1000, file.lastModified / 1000);
          } catch (error) { if (error.name !== "NotFoundError") throw error; }
        }
      }
    }
  }
  async service(rpc) {
    try {
      let value;
      switch (rpc.operation) {
        case "open": await this.hid.open(); break;
        case "close": await this.hid.close(); break;
        case "write": await this.hid.write(rpc.data); break;
        case "read": value = await this.hid.read(rpc.timeout); break;
        case "progress": this.job.message = rpc.message; break;
        case "view": this.job.view = rpc.view; break;
        case "persist": {
          const {plan, name} = await this.file(rpc.path, true);
          try {
            await plan.getFileHandle(name);
            throw new Error("An existing recovery file cannot be replaced.");
          } catch (error) { if (error.name !== "NotFoundError") throw error; }
          const handle = await plan.getFileHandle(name, {create: true});
          const stream = await handle.createWritable();
          try { await stream.write(rpc.data); await stream.close(); }
          catch (error) { await stream.abort().catch(() => {}); throw error; }
          break;
        }
        case "load": {
          const {plan, name} = await this.file(rpc.path);
          value = await (await (await plan.getFileHandle(name)).getFile()).text();
          break;
        }
        default: throw new Error("Unknown browser device operation.");
      }
      return {value: value ?? null};
    } catch (error) { return {error: error.message || String(error)}; }
  }
  async request(path, payload) {
    if (!payload) return this.job;
    if (this.job.status === "running") return {id: this.job.id, existing: true, message: this.job.message};
    if (this.reason) throw new Error(this.reason);
    if (payload.initial) {
      this.job = {id: crypto.randomUUID(), status: "done", result: {disconnected: true}};
      return {id: this.job.id};
    }
    if (new TextEncoder().encode(JSON.stringify(payload)).length > 48 * 1024 * 1024) throw new Error("The upload exceeds 48 MiB.");
    const selecting = ["refresh", "open_installer"].includes(payload.action);
    const selection = selecting ? this.hid.choose(this.forceChooser) : null;
    this.forceChooser = false;
    const recovery = ["apply_repack", "apply_offline_merge", "restore_repack_backup"].includes(payload.action) && !this.folder
      ? showDirectoryPicker({mode: "readwrite"}) : null;
    this.job = {id: crypto.randomUUID(), status: "running", message: "Opening offline Manager…"};
    const id = this.job.id;
    (async () => {
      try {
        const devices = selection ? await selection : null;
        if (recovery) this.folder = await recovery;
        await this.initialize();
        if (devices) await this.connect(devices);
        if (recovery) await this.restoreFiles();
        this.python.globals.set("browser_request", JSON.stringify(payload));
        const result = JSON.parse(await this.python.runPythonAsync("run_request(browser_request)"));
        if (result._browser_error) throw new Error(result._browser_error);
        this.job = {id, status: "done", result, connection_lost: Boolean(result.commit_pending)};
      } catch (error) {
        await this.hid.close().catch(() => {});
        this.job = {id, status: "error", message: error.message || String(error), connection_lost: true};
      }
    })();
    return {id};
  }
}
