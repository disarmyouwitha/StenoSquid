import {HidConnection} from "./hid.js";
import {createJsManager} from "./js_manager.js";
import {makeFactorySource, makePrepare, makeSourceReader} from "./factory.js";

export function unsupportedReason(scope = globalThis) {
  if (!scope.isSecureContext) return "Open the downloaded StenoSquid Manager HTML file in desktop Chrome or Edge.";
  if (!scope.navigator.hid) return "Direct keyboard access needs desktop Chrome or Edge. Safari and Firefox do not support WebHID.";
  return "";
}

export class BrowserBackend {
  constructor() {
    this.job = {id: "", status: "idle"};
    this.reason = unsupportedReason();
    this.folder = null;
    this.serial = null;
    this.forceChooser = false;
    this.hid = this.reason ? null : new HidConnection(navigator.hid, () => { this.job.connection_lost = true; });
    // The pinned factory dictionaries ride inside the page as a zip; the editor, installer and factory restore read them from there.
    let dictionaries = null;
    const archive = () => (dictionaries ??= globalThis.offlineAssets ? offlineBytes(offlineAssets.dictionaries) : null);
    this.js = this.hid ? createJsManager({hid: this.hid, factorySource: makeFactorySource(archive), prepare: makePrepare(archive), readSource: makeSourceReader(archive), recoveryStore: this.recoveryStore()}) : null;
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
        this.folder = folder;
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
  async connect({devices, explicit}) {
    const candidates = [];
    let chosenSerial = explicit ? null : this.serial;
    let firstError;
    for (const [index, device] of devices.entries()) {
      this.job.message = "Checking keyboard connections; preferring USB…";
      try {
        await this.hid.select([device]);
        const info = await this.js.probe();
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
  // The recovery folder the user chose, as the JavaScript Manager's store of verified repack backups (see repack_jobs.js).
  recoveryStore() {
    const backend = this;
    const directory = async (identity, token, create) => {
      if (!backend.folder) throw new Error("Choose a recovery folder before repacking or restoring dictionaries.");
      const keyboard = await backend.folder.getDirectoryHandle(identity, {create});
      return keyboard.getDirectoryHandle(token, {create});
    };
    return {
      async save(identity, token, name, text) {
        const plan = await directory(identity, token, true);
        const file = name + ".json";
        let exists = true;
        try { await plan.getFileHandle(file); } catch (error) { if (error.name !== "NotFoundError") throw error; exists = false; }
        if (exists) throw new Error("An existing recovery file cannot be replaced.");
        const stream = await (await plan.getFileHandle(file, {create: true})).createWritable();
        try { await stream.write(text); await stream.close(); }
        catch (error) { await stream.abort().catch(() => {}); throw error; }
      },
      async load(identity, token, name) {
        const plan = await directory(identity, token, false);
        return (await (await plan.getFileHandle(name + ".json")).getFile()).text();
      },
      async latest(identity) {
        if (!backend.folder) return null;
        let keyboard;
        try { keyboard = await backend.folder.getDirectoryHandle(identity); }
        catch (error) { if (error.name === "NotFoundError") return null; throw error; }
        let best = null, bestTime = -1;
        for await (const [token, plan] of keyboard.entries()) {
          if (plan.kind !== "directory" || !/^[a-f0-9]{32}$/.test(token)) continue;
          try {
            await plan.getFileHandle("original.json");
            const attempt = await (await plan.getFileHandle("attempt.json")).getFile();
            if (attempt.lastModified > bestTime) { best = token; bestTime = attempt.lastModified; }
          } catch (error) { if (error.name !== "NotFoundError") throw error; }
        }
        return best;
      },
    };
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
        if (devices) await this.connect(devices);
        let result;
        // A failure is reported to the page without dropping the connection.
        try {
          result = await this.js.run(payload, (message) => { this.job.message = message; }, (view) => { this.job.view = view; });
        } catch (error) { result = {_browser_error: error.message || String(error)}; }
        if (result._browser_error) {
          // Validation and stale-preview errors leave the device connected so
          // the draft can be corrected or reloaded without leaving the modal.
          this.job = {id, status: "error", message: result._browser_error,
            connection_lost: this.job.connection_lost === true};
          return;
        }
        this.job = {id, status: "done", result, connection_lost: Boolean(result.commit_pending)};
      } catch (error) {
        await this.hid.close().catch(() => {});
        this.job = {id, status: "error", message: error.message || String(error), connection_lost: true};
      }
    })();
    return {id};
  }
}
