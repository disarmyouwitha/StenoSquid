const node = (id) => document.getElementById(id);
const status = node("status");
const token = document.querySelector('[name="manager-token"]').content;
let state;
let setup;
let selectedId;
let journalUsage = {};
let flashMap;
let busy = false;
let connected = false;
let connectionTransport = "usb";
let connectionFailed = false;
let importToApply;
let optimizationToApply;
let cleanupToken;
let repackPlan;
const number = new Intl.NumberFormat();
const jobMessages = {
  open_installer: "Checking the dictionary installer…",
  setup: "Preparing the selected factory dictionaries…",
  preview_optimize: "Calculating compact storage…",
  apply_optimize: "Saving compact dictionary…", layout_map: "Scanning memory…",
  preview_import: "Calculating import size…", apply_import: "Importing dictionary…", import: "Importing dictionary…",
  backup: "Preparing backup…", export: "Exporting dictionary…",
  preview_package: "Checking factory dictionary space…", apply_package: "Saving factory dictionary…",
  preview_repack: "Preparing the USB repack preview…", apply_repack: "Preparing to repack dictionary flash…",
  restore_repack_backup: "Restoring the original dictionary backup…",
  preview_compact: "Finding blocks to reclaim…", compact: "Reclaiming flash space…", clear_personal: "Clearing personal dictionary…"
};

const bluetoothSpeedWarning = "Dictionary transfers over Bluetooth can take significantly longer than over USB. You can continue over Bluetooth or switch to USB for a faster transfer.";

function showBluetoothSpeedWarning(id, eligible = true) {
  const warning = node(id);
  warning.hidden = !eligible || connectionTransport !== "bluetooth";
  warning.textContent = warning.hidden ? "" : bluetoothSpeedWarning;
}

function notify(text, error = false) {
  status.textContent = text;
  status.dataset.error = error;
  const progress = node("job-progress");
  const percent = /^Writing .*?(?:… (\d+)%| \((\d+)%\))$/.exec(text);
  progress.hidden = !percent;
  if (percent) progress.value = Number(percent[1] ?? percent[2]);
}
function selection() {
  return state?.catalog.find((row) => row.id === selectedId);
}
function setConnection(isConnected, lost = false, transport = connectionTransport, serial) {
  connectionTransport = transport;
  connected = isConnected;
  connectionFailed = lost;
  const badge = node("connected");
  badge.hidden = !isConnected;
  if (isConnected) {
    badge.textContent = `Steno Squid left · ${transport === "bluetooth" ? "Bluetooth" : "USB"}`;
    badge.title = serial ? `Keyboard serial ${serial}` : "";
  }
  const button = node("refresh");
  button.textContent = isConnected ? "Disconnect" : "Connect";
  button.toggleAttribute("data-connected", isConnected);
  if (!isConnected) {
    clearImportPlan();
    state = undefined;
    setup = undefined;
    journalUsage = {};
    flashMap = undefined;
    clearRepackPlan();
    node("workspace").hidden = true;
    node("setup-panel").hidden = true;
  }
}
function controls() {
  node("job-status").toggleAttribute("data-busy", busy);
  node("job-status").querySelector(".job-spinner").hidden = !busy;
  const packages = [...node("setup-form").querySelectorAll("input")];
  for (const checkbox of packages) checkbox.disabled = busy;
  node("setup-submit").disabled = busy || !setup || !packages.some((checkbox) => checkbox.checked);
  node("repack-restore").disabled = busy || !setup?.repack_backup;
  node("repack-cancel").disabled = busy;
  const importFields = node("import").elements;
  for (const el of document.querySelectorAll("[data-write]")) el.disabled = busy || !state || state.read_only;
  const usbImport = connectionTransport === "usb";
  const importDisabled = busy || !state || state.read_only || !usbImport;
  importFields.file.disabled = importFields.name.disabled = importDisabled;
  node("import-preview").disabled = node("import-apply").disabled = importDisabled;
  node("import-usb-note").hidden = usbImport;
  for (const id of ["refresh", "backup", "export", "reprovision",
    "emergency-provision", "setup-cancel"]) node(id).disabled = busy;
  node("emergency-provision").hidden = connected || !connectionFailed;
  node("flash-map-scan").disabled = busy;
  const repack = node("repack-preview");
  repack.disabled = busy || !state?.repack;
  repack.title = connectionTransport === "bluetooth" ? "Connect over USB to repack" : "";
  if (!state) return;
  const row = selection();
  const rank = state.catalog.indexOf(row);
  const editable = !busy && !state.read_only;
  node("up").disabled = !editable || rank <= 0;
  node("down").disabled = !editable || rank < 0 || rank >= state.catalog.length - 1;
  const independent = row?.name !== "user_dictionary" && ["named", "snapshot"].includes(row?.kind);
  const builtIn = row?.name === "user_dictionary";
  const factory = state.factory_actions[row?.id];
  const uninstalled = row?.kind === "uninstalled";
  node("rename").disabled = !editable || !independent;
  const canClearPersonal = builtIn && (row.kind === "personal" || state.personal_clear);
  node("delete").hidden = uninstalled;
  node("delete").disabled = !editable || !(independent || canClearPersonal || (factory && row.kind === "base"));
  node("install-dictionary").hidden = !uninstalled || !factory;
  node("install-dictionary").disabled = !editable || !uninstalled || !factory;
  const journal = journalUsage[row?.id];
  const snapshot = row?.kind === "snapshot" && (builtIn || journal?.count > 0);
  const journalOnly = ["personal", "named"].includes(row?.kind);
  const compactSupported = snapshot ? state.adaptive_imports
    : builtIn ? state.personal_conversion : state.journal_conversion;
  node("optimize").hidden = !snapshot && !journalOnly;
  node("optimize").disabled = !editable || !compactSupported;
  node("export").disabled = busy || !row || uninstalled;
  node("target").textContent = uninstalled ? `${row.name} is uninstalled.` : row ? `Editing ${row.name}` : "Select a dictionary first.";
  const canRevert = row?.kind === "base";
  node("edit-note").hidden = !row;
  node("edit-revert-note").hidden = !canRevert;
  for (const el of node("edit").querySelectorAll("button")) {
    const unavailable = el.value === "reset" && !canRevert;
    el.hidden = unavailable;
    el.disabled = !editable || !row || uninstalled || unavailable;
  }
}
function render(newState, usage = {}) {
  state = newState;
  setup = undefined;
  node("setup-panel").hidden = true;
  clearImportPlan();
  clearOptimizationPlan();
  clearFactoryPlan();
  node("flash-map-panel").hidden = !state.layout_map;
  node("repack-preview").hidden = !state.repack && state.transport !== "bluetooth";
  updateImportControls();
  if (!selection()) selectedId = state.catalog[0]?.id;
  node("catalog").replaceChildren();
  for (const [index, row] of state.catalog.entries()) {
    const tr = node("catalog").insertRow();
    tr.insertCell().textContent = String(index + 1);
    const label = document.createElement("label");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "dictionary";
    radio.checked = selectedId === row.id;
    radio.onchange = () => { clearOptimizationPlan(); clearFactoryPlan(); selectedId = row.id; controls(); };
    label.append(radio, ` ${row.name}${row.kind === "uninstalled" ? " (uninstalled)" : ""}`);
    tr.insertCell().append(label);
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = row.enabled;
    if (row.kind === "uninstalled") checkbox.disabled = true;
    else checkbox.dataset.write = "";
    checkbox.setAttribute("aria-label", `Enable ${row.name}`);
    checkbox.onchange = () => {
      const enabled = checkbox.checked;
      checkbox.checked = row.enabled;
      run({action: "enable", id: row.id, enabled});
    };
    tr.insertCell().append(checkbox);
    const compiled = ["base", "snapshot"].includes(row.kind);
    const journal = row.kind !== "uninstalled" ? usage[row.id] : undefined;
    const entries = tr.insertCell();
    const size = tr.insertCell();
    const count = compiled ? row.base_entries : journal?.entries;
    const bytes = compiled ? row.image_bytes + (journal?.bytes ?? 0) : journal?.bytes;
    entries.textContent = count == null ? "—" : number.format(count);
    size.textContent = bytes == null ? "—" : formatBytes(bytes);
    if (compiled && journal?.count) {
      entries.title = `Compiled entry count. ${number.format(journal.count)} live journal records include saved edits and deletions.`;
    } else if (journal) {
      entries.title = "Journal translations. Saved deletions are excluded.";
    } else if (!compiled && row.kind !== "uninstalled") {
      entries.title = size.title = "Journal totals appear after the keyboard is read.";
    }
    if (bytes != null) {
      const contents = compiled ? (journal?.bytes ? "compiled image and live journal records" : "compiled image")
        : "live journal records";
      size.title = `${number.format(bytes)} bytes in the ${contents}. Shared indexes and obsolete records are excluded.`;
    }
    const edits = tr.insertCell();
    const savedEdits = compiled || row.kind === "uninstalled" ? usage[row.id]?.count : undefined;
    edits.textContent = savedEdits == null ? "—" : number.format(savedEdits);
    if (savedEdits != null) edits.title = "Live saved changes to the compiled baseline, including deletions.";
  }
  node("layout").replaceChildren(...state.layouts.map((layout) => new Option(layout, layout)));
  node("layout").value = state.layout;
  setConnection(true, false, state.transport ?? "usb", state.serial);
  node("workspace").hidden = false;
}
async function request(path, payload) {
  const options = {headers: {"X-StenoSquid-Token": token}};
  if (payload) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (payload && !response.ok && result.error === "A keyboard operation is already running.") {
    const job = await request(path);
    return {id: job.id, existing: true, message: job.status === "running" ? job.message : ""};
  }
  if (!response.ok) throw new Error(result.error || "Manager request failed.");
  return result;
}
function download(filename, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2) + "\n"], {type: "application/json"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function showView(result) {
  if (result.usage || result.usage_error) journalUsage = result.usage ?? {};
  render(result.state, journalUsage);
  const usageError = node("journal-size-error");
  if (result.usage || result.usage_error) usageError.hidden = !result.usage_error;
  if (result.usage_error) usageError.textContent = `Journal sizes unavailable. ${result.usage_error.replace(/^ERR\s+/, "")}`;
  if (result.usage || result.usage_error) node("journal-budget").hidden = !result.journal_budget_exceeded;
  if (result.layout_map) {
    flashMap = result.layout_map;
    showFlashMap(flashMap);
  }
  if (result.layout_map_error) showMapError(result.layout_map_error);
}
async function watch(id) {
  let job;
  let viewShown = false;
  do {
    await new Promise((resolve) => setTimeout(resolve, 550));
    job = await request("/api/job");
    if (job.id !== id) throw new Error("Operation status changed. Refresh the keyboard to check its saved state.");
    if (job.status === "running" && job.view && !viewShown) {
      journalUsage = {};
      showView(job.view);
      controls();
      viewShown = true;
    }
    if (job.message && job.message !== "Opening keyboard connection…" && job.status === "running") notify(job.message);
  } while (job.status === "running");
  if (job.status === "error") throw new Error(job.message);
  const result = job.result;
  if (result.disconnected) {
    setConnection(false);
    notify("Plug in your left half or pair it in your computer’s Bluetooth settings.");
  } else if (result.commit_pending) {
    setConnection(false, true);
    clearImportPlan();
    notify("The keyboard accepted the change. Refresh the connection to verify it before retrying.", true);
  } else if (result.state?.setup) {
    state = undefined;
    journalUsage = {};
    setup = result.state.setup;
    node("workspace").hidden = true;
    node("setup-panel").hidden = false;
    const recovering = Boolean(setup.repack_backup);
    node("setup-title").textContent = recovering ? "Recover dictionaries"
      : setup.reprovision ? "Reprovision dictionaries" : "Set up dictionaries";
    node("setup-description").textContent = recovering
      ? "Restore the original backup saved on this computer before the repack."
      : "Choose the factory dictionaries to install. You can add more later.";
    node("setup-form").hidden = recovering;
    node("repack-restore").hidden = !recovering;
    node("setup-warning").textContent = setup.reprovision
      ? "Reprovisioning erases all dictionaries and saved edits on this keyboard. Download a full backup first if you want to keep them."
      : "Setup clears the external dictionary pool. Keep the left half connected until it reboots.";
    node("setup-cancel").hidden = !setup.reprovision;
    node("setup-packages").replaceChildren(...Object.entries(setup.packages ?? {}).map(([key, name]) => {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = key;
      checkbox.checked = key === "lapwing-base";
      label.append(checkbox, name);
      return label;
    }));
    setConnection(true, false, result.state.transport ?? "usb", setup.serial);
    showBluetoothSpeedWarning("setup-speed-warning", !recovering);
    notify(recovering ? "The original repack backup is available."
      : setup.reprovision ? "Choose the factory dictionaries to reinstall." : "Connected. The keyboard needs dictionary setup.");
  } else if (result.state) {
    showView(result);
    if (result.package_changed) {
      const plan = result.package_changed;
      notify(`${plan.name} ${plan.restoring ? "installed. Use the Active checkbox to enable it." : "removed from flash."}`);
    } else if (result.repacked || result.restored) {
      notify(result.repacked ? "Repack complete. Dictionaries verified."
        : "Original backup restored. Dictionaries verified.");
    } else if (result.installed) {
      notify("Dictionaries installed. The keyboard is ready.");
    } else if (result.imported) {
      notify(`Imported ${number.format(result.imported)} ${result.imported === 1 ? "entry" : "entries"}.`);
    } else if (result.cleared) {
      notify("Personal dictionary cleared.");
    } else if (result.optimized) {
      notify(`Saved the compact baseline for ${result.optimized}. The keyboard is ready.`);
    } else {
      notify(state.read_only ? "Connected. The keyboard is in read-only recovery." : "Keyboard ready.");
    }
  } else if (result.download) {
    download(result.filename, result.download);
    notify(`Downloaded ${result.filename}${result.count === undefined ? "" : ` with ${number.format(result.count)} entries`}.`);
  } else if (result.import_plan) {
    showImportPlan(result.import_plan);
  } else if (result.package_plan) {
    showFactoryPlan(result.package_plan);
  } else if (result.repack_plan) {
    showRepackPlan(result.repack_plan);
  } else if (result.optimization_plan) {
    showOptimizationPlan(result.optimization_plan);
  } else if (result.cleanup) {
    const {token, reclaimable} = result.cleanup;
    cleanupToken = token;
    if (result.layout_map) {
      const map = result.layout_map;
      if (flashMap && map.generation === flashMap.generation) map.journal_usage = flashMap.journal_usage;
      showFlashMap(map, reclaimable);
    }
    node("journal-summary").textContent = reclaimable.length
      ? "Reclaim makes these blocks available. The left half will reboot."
      : "No blocks can be reclaimed in this pass.";
    node("journal-reclaim-blocks").replaceChildren(
      ...(reclaimable.length ? segmentCost(reclaimable.length, null, "blocks to free") : []));
    node("compact-apply").hidden = !token;
    node("journal-preview").hidden = false;
    notify(token ? "Review blocks to reclaim." : "No blocks can be reclaimed in this pass.");
  } else if (result.layout_map) {
    showFlashMap(result.layout_map);
    notify("Memory scan complete.");
  }
}
function formatBytes(bytes) {
  return bytes < 1024 ? `${number.format(bytes)} B` : `${number.format(Math.round(bytes / 102.4) / 10)} KiB`;
}
const factoryColors = {
  2: "#9bdef9", 3: "#63bbf3", 4: "#4a96e8", 5: "#5570dc", 6: "#3046a7", 7: "#e98658",
};

function showFlashMap(map, reclaimable = []) {
  node("flash-map-error").hidden = true;
  const summary = node("flash-map-summary");
  summary.textContent = map.free_segments === null
    ? "Free space is unknown. A catalog is unavailable or the store is in read-only recovery."
    : map.staging_blocked ? "A retained catalog reader currently blocks publication." : "";
  summary.hidden = !summary.textContent;
  const grid = node("flash-map-grid");
  const legend = node("flash-map-legend");
  grid.setAttribute("aria-label", map.projected ? "Projected flash layout, 256 blocks" : "Current flash layout, 256 blocks");
  const result = node("flash-map-result");
  result.toggleAttribute("data-projected", !!map.projected);
  const labels = {c: "Catalog roots", x: "Reserved", b: "Unidentified pinned image", s: "Unidentified user snapshot",
    h: "Journal write head", j: map.projected ? "Packed journal" : "Unattributed journal blocks",
    "empty-journal": "Pending cleanup", reclaimable: "Reclaimable",
    f: "Forward index", r: "Reverse index", "retained-journal": "Retained-only journal blocks",
    retained: "Retained-only extents", ".": map.projected ? "Free after repack" : "Unreferenced", "?": "Unknown"};
  const colorFor = (id, name) => {
    if (name === "user_dictionary") return "#d0b961";
    if (factoryColors[id]) return factoryColors[id];
    return `hsl(${(id * 137.508) % 360} 58% ${id % 2 === 0 ? 67 : 58}%)`;
  };
  const bySegment = new Map();
  const dictionaries = new Map();
  const kinds = new Set();
  const candidates = new Set(reclaimable);
  const images = (map.images ?? []).filter((image) =>
    map.segments.slice(image.start, image.start + image.segments).every((segment) => segment.kind === image.kind)
  );
  for (const image of images) {
    dictionaries.set(image.id, {name: image.name, color: colorFor(image.id, image.name)});
    for (let i = image.start; i < image.start + image.segments; i++) bySegment.set(i, [dictionaries.get(image.id)]);
  }
  for (const {id, name} of state?.catalog ?? []) {
    for (const segment of Object.keys(map.journal_usage?.[id]?.blocks ?? {}).map(Number)) {
      if (!["j", "h"].includes(map.segments[segment]?.kind)) continue;
      if (!dictionaries.has(id)) dictionaries.set(id, {name, color: colorFor(id, name)});
      if (!bySegment.has(segment)) bySegment.set(segment, []);
      bySegment.get(segment).push(dictionaries.get(id));
    }
  }
  grid.replaceChildren(...map.segments.map((segment) => {
    const cell = document.createElement("span");
    const owners = bySegment.get(segment.index);
    const canReclaim = ["j", "retained-journal"].includes(segment.kind) && candidates.has(segment.index);
    const kind = canReclaim ? "reclaimable"
      : segment.kind === "j" && map.journal_usage && !owners ? "empty-journal" : segment.kind;
    cell.dataset.kind = kind;
    const description = owners
      ? owners.map(({name}) => name).join(" / ") + (segment.kind === "h" ? " · Write head" : "")
      : labels[kind];
    cell.title = `Segment ${segment.index} · ${description}`;
    if (canReclaim && owners) cell.title += " · Reclaimable";
    if (canReclaim || !owners) kinds.add(kind);
    else if (owners.length === 1) cell.style.backgroundColor = owners[0].color;
    else {
      const stops = owners.map(({color}, index) =>
        `${color} ${100 * index / owners.length}% ${100 * (index + 1) / owners.length}%`);
      cell.style.setProperty("--block-colors", `linear-gradient(to right, ${stops.join(", ")})`);
    }
    return cell;
  }));
  legend.replaceChildren(...Object.entries(labels).filter(([kind]) => kinds.has(kind)).map(([kind, label]) => {
    const item = document.createElement("li");
    const marker = document.createElement("span");
    marker.dataset.kind = kind;
    item.append(marker, label);
    return item;
  }));
  for (const {name, color} of dictionaries.values()) {
    const item = document.createElement("li");
    const marker = document.createElement("span");
    marker.style.backgroundColor = color;
    item.append(marker, name);
    legend.append(item);
  }
  result.hidden = false;
}

function showMapError(message) {
  const failure = node("flash-map-error");
  failure.textContent = /scan deferred/i.test(message)
    ? "Flash is still unavailable. Try Scan Memory again."
    : message.replace(/^ERR\s+/, "");
  failure.hidden = false;
  node("flash-map-result").hidden = true;
}
function updateImportControls() {
  const fields = node("import").elements;
  const named = fields.name.value.trim() !== "";
  node("import-preview").textContent = named ? "Preview new dictionary" : "Import into personal";
}
function showImportError(message = "") {
  const error = node("import-error");
  error.hidden = !message;
  error.textContent = message;
  if (message) error.scrollIntoView({block: "nearest"});
}
function clearImportPlan() {
  importToApply = undefined;
  node("import-plan").hidden = true;
  node("import-preview").hidden = false;
}
function segmentCost(imageSegments, stagingSegments, imageLabel) {
  const blocks = document.createElement("div");
  blocks.className = "import-segments";
  blocks.setAttribute("aria-hidden", "true");
  for (let i = 0; i < Math.max(stagingSegments, imageSegments); i++) {
    const segment = document.createElement("span");
    segment.className = i < imageSegments ? "segment-added" : "segment-temporary";
    blocks.append(segment);
  }
  const caption = document.createElement("p");
  caption.className = "import-segment-caption";
  if (stagingSegments === null) {
    caption.textContent = `${imageSegments} ${imageLabel}`;
    return [blocks, caption];
  }
  const image = document.createElement("span");
  image.className = "caption-added";
  image.textContent = `${imageSegments} ${imageLabel}`;
  const staging = document.createElement("span");
  staging.className = "caption-staging";
  const temporary = Math.max(0, stagingSegments - imageSegments);
  staging.textContent = `${temporary} to stage`;
  staging.title = "Temporary blocks in addition to the blocks added";
  caption.append(image);
  if (temporary) caption.append(staging);
  return [blocks, caption];
}

function showImportPlan(plan) {
  if (!importToApply) {
    notify("Preview the current dictionary file.");
    return;
  }
  node("import-entry-count").textContent = `${number.format(plan.entries)} ${plan.entries === 1 ? "entry" : "entries"} to import`;

  const size = node("import-compact-size");
  size.textContent = formatBytes(plan.bytes);
  size.title = `${number.format(plan.bytes)} bytes`;
  const blocks = plan.staging_segments == null
    ? segmentCost(plan.image_segments, null, "image blocks")
    : segmentCost(plan.image_segments, plan.staging_segments, "added");
  node("import-plan-blocks").replaceChildren(...blocks);
  importToApply.token = plan.token;
  node("import-apply").hidden = !plan.token;
  node("import-plan").hidden = false;
  node("import-preview").hidden = true;
  notify(plan.token ? "Review the import preview." : "Import stopped.");
  if (!plan.token) showImportError(state?.repack
    ? `${plan.reason} ${connectionTransport === "bluetooth" ? "Switch to USB to preview a repack."
      : "Preview USB repack under External flash map to check for a larger free run."}`
    : plan.reason);
}
function clearOptimizationPlan() {
  optimizationToApply = undefined;
  node("optimization-plan").hidden = true;
}
function showOptimizationPlan(plan) {
  if (optimizationToApply?.id !== plan.id || optimizationToApply.operation !== plan.operation) return;
  optimizationToApply.token = plan.token;
  node("optimization-name").textContent = plan.name;
  const maxBytes = Math.max(plan.before_bytes, plan.after_bytes, 1);
  for (const [id, bytes] of [["optimization-before", plan.before_bytes], ["optimization-after", plan.after_bytes]]) {
    const size = node(id);
    size.textContent = formatBytes(bytes);
    size.title = `${number.format(bytes)} bytes in the image and live saved edits`;
    size.nextElementSibling.style.width = `${bytes / maxBytes * 100}%`;
  }
  const saved = plan.before_bytes - plan.after_bytes;
  const savings = node("optimization-savings");
  savings.dataset.saves = String(saved > 0);
  savings.textContent = saved > 0 ? `${formatBytes(saved)} saved`
    : saved < 0 ? `${formatBytes(-saved)} larger` : "No change";
  const blocks = Math.abs(plan.segment_delta);
  const allocation = blocks === 0 ? "No change in allocated flash blocks"
    : `${blocks} ${plan.segment_delta < 0 ? "fewer" : "more"} allocated flash ${blocks === 1 ? "block" : "blocks"}`;
  const noChange = plan.reason ?? "No saved edits to consolidate.";
  node("optimization-allocation").textContent = plan.token ? `${allocation} · ${plan.staging_segments} to stage` : noChange;
  node("optimization-apply").hidden = !plan.token;
  node("optimization-publish").hidden = !plan.token;
  node("optimization-plan").hidden = false;
  notify(plan.token ? "Review before compacting." : noChange);
}
function clearRepackPlan() {
  if (!repackPlan) return;
  repackPlan = undefined;
  node("flash-map-title").textContent = "External flash map";
  node("flash-map-actions").hidden = false;
  node("repack-actions").hidden = true;
  node("repack-plan").hidden = true;
  if (flashMap) showFlashMap(flashMap);
  else node("flash-map-result").hidden = true;
}
function showRepackPlan(plan) {
  repackPlan = plan;
  const {before, after, token} = plan;
  node("flash-map-title").textContent = "Projected layout after repack";
  node("flash-map-actions").hidden = true;
  node("repack-actions").hidden = false;
  showFlashMap(after);
  node("repack-run").textContent = `${before.largest_free_run} → ${after.largest_free_run} blocks`;
  node("repack-free").textContent = `${before.free_segments} → ${after.free_segments} blocks`;
  node("repack-detail").textContent = token
    ? "After you confirm, Manager backs up the dictionaries, verifies the packed layout, and rewrites the dictionary flash. Keep USB connected."
    : "This preview shows no space gain.";
  node("repack-apply").hidden = !token;
  node("repack-plan").hidden = false;
  notify(token ? "Review the USB repack preview." : "The dictionary layout is already well packed.");
}
function confirmRepack(action, serial, token) {
  if (busy) return;
  const message = action === "apply_repack"
    ? "Back up and repack this keyboard? Manager will verify the backup before rewriting the dictionary flash. Keep USB connected."
    : "Restore the original backup to this keyboard? This rewrites the dictionary flash. Keep USB connected.";
  if (window.confirm(message)) run({action, serial, token, confirm: serial});
}
async function run(payload) {
  if (busy) return;
  const importing = ["import", "preview_import", "apply_import"].includes(payload.action);
  if (importing) showImportError();
  clearRepackPlan();
  if (payload.action !== "preview_optimize") clearOptimizationPlan();
  clearFactoryPlan();
  if (cleanupToken && flashMap) showFlashMap(flashMap);
  cleanupToken = undefined;
  node("journal-preview").hidden = true;
  node("compact-apply").hidden = true;
  busy = true;
  controls();
  notify(jobMessages[payload.action] ?? "Connecting to the keyboard…");
  try {
    const {id, existing, message} = await request("/api/job", payload);
    if (existing) {
      if (payload.action === "preview_import") clearImportPlan();
      if (payload.action === "preview_optimize") clearOptimizationPlan();
      notify(message || "Checking the current keyboard operation…");
    }
    await watch(id);
  } catch (error) {
    if (payload.action === "refresh") setConnection(false, true);
    if (connected && /^(No reply to |HID (?:read|write) failed|Connect one Steno Squid left half|Pair and connect one Steno Squid left half|The paired keyboard’s Bluetooth Manager console)/.test(error.message)) {
      setConnection(false, true);
    }
    if (["apply_repack", "restore_repack_backup"].includes(payload.action)) {
      setConnection(false, true);
      notify(`${error.message} Refresh Connection to check the keyboard and recover if needed.`, true);
    } else if (importing) {
      notify("Import stopped.");
      showImportError(error.message);
    } else if (payload.action === "layout_map") {
      showMapError(error.message);
      notify("Memory scan stopped.", true);
      node("flash-map-error").focus();
    } else {
      if (payload.action === "refresh" && setup) {
        setup = undefined;
        node("setup-panel").hidden = true;
      }
      if (payload.action === "open_installer" && !connected) {
        notify(`Emergency provisioning needs a responding dictionary installer. ${error.message}`, true);
      } else {
        notify(error.message, true);
      }
    }
  } finally {
    busy = false;
    node("job-progress").hidden = true;
    controls();
  }
}
node("refresh").onclick = () => run(connected ? {action: "disconnect"} : {action: "refresh", transport: "auto"});
node("backup").onclick = () => run({action: "backup"});
node("reprovision").onclick = node("emergency-provision").onclick = () => run({action: "open_installer"});
node("setup-cancel").onclick = () => run({action: "refresh"});
node("repack-preview").onclick = () => run({action: "preview_repack"});
node("repack-cancel").onclick = () => {
  clearRepackPlan();
  node("repack-preview").focus();
  notify("Showing the current flash layout.");
};
node("repack-apply").onclick = () => {
  if (repackPlan?.token) confirmRepack("apply_repack", repackPlan.serial, repackPlan.token);
};
node("repack-restore").onclick = () => {
  if (setup?.repack_backup) confirmRepack("restore_repack_backup", setup.serial, setup.repack_backup);
};
node("compact").onclick = () => run({action: "preview_compact"});
node("compact-apply").onclick = () => {
  if (cleanupToken) run({action: "compact", token: cleanupToken});
};
node("optimize").onclick = () => {
  const row = selection();
  clearOptimizationPlan();
  const operation = row.kind === "personal" ? "personal" : row.kind === "named" ? "convert" : "consolidate";
  node("optimization-description").textContent = operation === "consolidate"
    ? "Fold saved edits into the compact dictionary." : "Convert journal entries into a compact dictionary.";
  optimizationToApply = {id: row.id, operation};
  run({action: "preview_optimize", ...optimizationToApply});
};
node("optimization-apply").onclick = () => {
  if (optimizationToApply?.token) run({action: "apply_optimize", ...optimizationToApply});
};
node("optimization-cancel").onclick = clearOptimizationPlan;
function scanLayout() {
  node("flash-map-error").hidden = true;
  return run({action: "layout_map"});
}
node("flash-map-scan").onclick = scanLayout;
let factoryPlan;

node("setup-form").onchange = controls;
node("setup-form").onsubmit = (event) => {
  event.preventDefault();
  if (busy || !setup) return;
  const packages = [...node("setup-form").querySelectorAll("input:checked")].map((input) => input.value);
  if (!packages.length) return;
  const confirmSerial = setup.reprovision
    ? window.prompt(`This erases all dictionaries and saved edits on the connected keyboard. Type its serial number ${setup.serial} to confirm.`)
    : null;
  const approved = setup.reprovision ? confirmSerial === setup.serial
    : window.confirm(`Install the selected dictionaries on ${setup.serial}? This clears its external dictionary pool.`);
  if (approved) run({action: "setup", serial: setup.serial, expected_status: setup.expected_status,
    confirm_serial: confirmSerial, packages, approved});
};

function clearFactoryPlan() {
  factoryPlan = null;
  node("factory-preview").hidden = true;
}

function showFactoryPlan(plan) {
  factoryPlan = plan;
  const cost = node("factory-cost");
  cost.style.setProperty("--segment-color", plan.restoring ? factoryColors[plan.dictionary_id] : "#65d0a3");
  const heading = document.createElement("div");
  heading.className = "import-format-heading";
  const name = document.createElement("strong");
  name.textContent = plan.name;
  const free = document.createElement("span");
  free.className = "import-bytes";
  free.textContent = `${number.format(plan.free_now)} → ${number.format(plan.free_after_both_roots)} free blocks`;
  heading.append(name, free);
  const count = plan.restoring ? plan.package_segments : plan.package_segments_released;
  const released = count === 1 ? "block freed" : "blocks freed";
  cost.replaceChildren(heading, ...segmentCost(count, plan.restoring ? plan.staging_segments : null,
    plan.restoring ? "added" : released));
  node("factory-summary").textContent = plan.restoring
    ? "Starts disabled. The left half will reboot."
    : "Saved edits are kept. The left half will reboot.";
  showBluetoothSpeedWarning("factory-speed-warning", plan.restoring);
  node("factory-apply").textContent = `${plan.restoring ? "Install" : "Delete"} ${plan.name}`;
  node("factory-preview").hidden = false;
  notify("Review the change in Dictionaries.");
}

node("install-dictionary").onclick = () => run({
  action: "preview_package", package: state.factory_actions[selectedId], restore: true
});
node("factory-apply").onclick = () => {
  if (factoryPlan) run({action: "apply_package", package: factoryPlan.package, restore: factoryPlan.restoring,
    token: factoryPlan.confirmation});
};
node("factory-cancel").onclick = () => {
  clearFactoryPlan();
  notify("Keyboard ready.");
};
node("layout").onchange = () => {
  const layout = node("layout").value;
  node("layout").value = state.layout;
  run({action: "layout", layout});
};
node("up").onclick = () => run({action: "move", id: selectedId, position: state.catalog.indexOf(selection())});
node("down").onclick = () => run({action: "move", id: selectedId, position: state.catalog.indexOf(selection()) + 2});
node("export").onclick = () => run({action: "export", id: selectedId});
node("rename").onclick = () => {
  const name = window.prompt("New dictionary name", selection().name);
  if (name) run({action: "rename", id: selectedId, name});
};
node("delete").onclick = () => {
  const {name, kind} = selection();
  const factory = state.factory_actions[selectedId];
  if (kind === "base" && factory) {
    run({action: "preview_package", package: factory, restore: false});
    return;
  }
  if (name === "user_dictionary") {
    const confirm = window.prompt("Type CLEAR to remove every personal entry and saved edit. Other dictionaries stay in place.");
    if (confirm === "CLEAR") run({action: "clear_personal", id: selectedId, confirm});
    return;
  }
  const confirm = window.prompt(`Type ${name} to delete it and its entries from the active catalog.`);
  if (confirm === name) run({action: "delete", id: selectedId, confirm});
};
node("edit").onsubmit = (event) => {
  event.preventDefault();
  const fields = new FormData(event.currentTarget);
  run({action: event.submitter.value, id: selectedId, outline: fields.get("outline").trim(), translation: fields.get("translation")});
};
node("import").oninput = (event) => {
  const form = event.currentTarget;
  const file = event.target.name === "file" && event.target.files[0];
  if (file) {
    const stem = file.name.replace(/\.json$/i, "").normalize("NFKD").replace(/\p{M}/gu, "");
    form.elements.name.value = stem.replace(/[^A-Za-z0-9 ._-]+/g, "-")
      .replace(/^[^A-Za-z0-9]+/, "").slice(0, 44).trimEnd() || "Imported dictionary";
  }
  showImportError();
  clearImportPlan();
  updateImportControls();
  if (file) form.requestSubmit();
};
node("import-apply").onclick = () => {
  if (connectionTransport === "usb" && importToApply?.token) {
    const approved = importToApply;
    clearImportPlan();
    run({action: "apply_import", ...approved});
  }
};
node("import").onsubmit = async (event) => {
  event.preventDefault();
  if (node("import-preview").disabled || node("import-preview").hidden) return;
  const {file: fileInput, name: nameInput} = event.currentTarget.elements;
  const file = fileInput.files[0];
  const name = nameInput.value.trim();
  if (!file) return;
  try {
    const source = await file.text();
    if (node("import-preview").disabled || fileInput.files[0] !== file || nameInput.value.trim() !== name) return;
    if (name) {
      clearImportPlan();
      importToApply = {source, name};
      await run({action: "preview_import", ...importToApply});
    } else {
      const confirmation = "Import this file into your personal dictionary? Entries with matching outlines will be replaced.";
      if (!window.confirm(confirmation)) return;
      await run({action: "import", source});
    }
  } catch (error) {
    showImportError(error.message);
  }
};

controls();
setInterval(async () => {
  if (!connected || busy) return;
  let lost = false;
  try {
    lost = (await request("/api/job")).connection_lost === true;
  } catch {
    lost = true;
  }
  if (lost && connected && !busy) {
    setConnection(false, true);
    controls();
    notify("Keyboard disconnected. Select Connect to try again.", true);
  }
}, 2000);
request("/api/job").then(async (job) => {
  if (busy) return;
  if (job.status !== "running") {
    await run({action: "refresh", transport: "auto", initial: true});
    return;
  }
  busy = true;
  controls();
  notify(job.message || "Working on the keyboard…");
  try {
    await watch(job.id);
  } catch (error) {
    notify(error.message, true);
  } finally {
    busy = false;
    node("job-progress").hidden = true;
    controls();
  }
}).catch((error) => notify(error.message, true));
