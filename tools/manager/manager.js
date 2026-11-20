const node = (id) => document.getElementById(id);
const status = node("status");
const token = document.querySelector('[name="manager-token"]').content;
let state;
let selectedId;
let busy = false;
let importToApply;
let optimizationToApply;
const number = new Intl.NumberFormat();

function notify(text, error = false) {
  status.textContent = text;
  status.dataset.error = error;
}
function selection() {
  return state?.catalog.find((row) => row.id === selectedId);
}
function controls() {
  for (const el of document.querySelectorAll("[data-write]")) el.disabled = busy || !state || state.read_only;
  for (const id of ["refresh", "backup", "backup-complete", "updates", "export", "restore-submit", "measure"]) node(id).disabled = busy;
  node("flash-map-scan").disabled = busy;
  if (!state) return;
  const row = selection();
  const rank = state.catalog.indexOf(row);
  const editable = !busy && !state.read_only;
  node("up").disabled = !editable || rank <= 0;
  node("down").disabled = !editable || rank < 0 || rank >= state.catalog.length - 1;
  const independent = row?.name !== "user_dictionary" && ["named", "snapshot"].includes(row?.kind);
  for (const id of ["rename", "delete"]) {
    node(id).disabled = !editable || !independent;
  }
  const note = node("dictionary-actions-note");
  note.hidden = !row || independent;
  note.textContent = row?.name === "user_dictionary"
    ? "The personal dictionary is built in. You can edit and export its entries here."
    : "This is a factory dictionary. You can turn it off or change its priority.";
  const snapshot = state.adaptive_imports && row?.kind === "snapshot";
  const personal = state.personal_conversion && row?.kind === "personal";
  node("optimize").hidden = !snapshot && !personal;
  node("optimize").disabled = !editable;
  node("optimize").textContent = snapshot ? "Preview consolidation" : "Preview conversion";
  node("replace-snapshot").hidden = !snapshot;
  node("export").disabled = busy || !row;
  node("target").textContent = row ? `Editing ${row.name}` : "Select a dictionary first.";
  for (const el of node("edit").querySelectorAll("button")) {
    el.disabled = !editable || !row || (el.value === "reset" && row.kind !== "base");
  }
}
function render(newState, usage = {}) {
  state = newState;
  clearImportPlan();
  clearOptimizationPlan();
  node("flash-map-panel").hidden = !state.layout_map;
  node("flash-map-result").hidden = true;
  node("import-advanced").hidden = !state.adaptive_imports;
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
    radio.onchange = () => { clearOptimizationPlan(); selectedId = row.id; controls(); };
    label.append(radio, ` ${row.name}`);
    tr.insertCell().append(label);
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = row.enabled;
    checkbox.dataset.write = "";
    checkbox.setAttribute("aria-label", `Enable ${row.name}`);
    checkbox.onchange = () => {
      const enabled = checkbox.checked;
      checkbox.checked = row.enabled;
      run({action: "enable", id: row.id, enabled});
    };
    tr.insertCell().append(checkbox);
    tr.insertCell().textContent = ["base", "snapshot"].includes(row.kind) ? number.format(row.base_entries) : "—";
    tr.insertCell().textContent = row.image_bytes ? `${number.format(Math.ceil(row.image_bytes / 1024))} KiB` : "—";
    const saved = usage[row.id];
    tr.insertCell().textContent = saved ? `${number.format(saved.count)} · ${number.format(saved.bytes)} B` : "—";
  }
  node("layout").replaceChildren(...state.layouts.map((layout) => new Option(layout, layout)));
  node("layout").value = state.layout;
  node("refresh").textContent = "Refresh connection";
  node("workspace").hidden = false;
  node("backup-complete").hidden = !state.adaptive_imports;
  node("maintenance").textContent = `${state.generation === null ? "" : `Catalog generation ${state.generation} · `}${state.maintenance_active ? "Maintenance active · " : ""}${state.tail} pending index edits · ${state.auto_checkpoints} automatic checkpoints · ${state.auto_reclaimed} journal segments freed this boot. Compiled images may be shared by several dictionaries.`;
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
async function watch(id) {
  let job;
  do {
    await new Promise((resolve) => setTimeout(resolve, 550));
    job = await request("/api/job");
    if (job.id !== id) throw new Error("Operation status changed. Refresh the keyboard to check its saved state.");
    if (job.message && job.status === "running") notify(job.message);
  } while (job.status === "running");
  if (job.status === "error") throw new Error(job.message);
  const result = job.result;
  if (result.state) {
    render(result.state, result.usage);
    if (result.updated) {
      node("apply-updates").hidden = true;
      const summary = result.updated.length ? `Updated ${result.updated.join(", ")}.` : "All installed bases already match.";
      if (result.conflicts.length) {
        download("stenosquid-update-conflicts.json", result.conflicts);
        notify(`${summary} Kept ${result.conflicts.length} local edits over upstream changes. Conflict report downloaded.`);
      } else {
        notify(summary);
      }
    } else if (result.optimized) {
      notify(`Saved the compact baseline for ${result.optimized}. The keyboard is ready.`);
    } else if (result.restored) {
      notify("Backup restored. The keyboard is ready.");
    } else {
      notify(state.read_only ? "Connected. The keyboard is in read-only recovery." : "Keyboard ready.");
    }
  } else if (result.download) {
    download(result.filename, result.download);
    notify(`Downloaded ${result.filename}${result.count === undefined ? "" : ` with ${number.format(result.count)} entries`}.`);
  } else if (result.updates) {
    node("apply-updates").hidden = !result.updates.length;
    notify(result.updates.length
      ? `Pinned updates available for ${result.updates.join(", ")}.`
      : "All installed bases match this version’s pinned sources.");
  } else if (result.import_plan) {
    showImportPlan(result.import_plan);
  } else if (result.optimization_plan) {
    showOptimizationPlan(result.optimization_plan);
  } else if (result.layout_map) {
    showFlashMap(result.layout_map);
  }
}
function showFlashMap(map) {
  const summary = node("flash-map-summary");
  summary.textContent = map.free_segments === null
    ? "Free space is unknown. A catalog is unavailable or the store is in read-only recovery."
    : `${map.free_segments} free segments, ${map.largest_free_run} contiguous, ${map.reserve} held in reserve.`;
  if (map.free_segments !== null) {
    const unavailable = map.segments.length - 3 - map.free_segments;
    summary.textContent += ` ${unavailable} referenced data segments, plus 2 roots and 1 diagnostic segment.`;
    if (map.staging_blocked) summary.textContent += " A retained catalog reader currently blocks publication.";
  }
  const grid = node("flash-map-grid");
  const labels = {c: "Catalog roots", x: "Reserved", b: "Pinned images", s: "User snapshots", h: "Latest journal",
    j: "Journals", f: "Forward index", r: "Reverse index", "retained-journal": "Retained-only journals",
    retained: "Retained-only extents", ".": "Unreferenced", "?": "Unknown"};
  grid.replaceChildren(...map.segments.map((segment) => {
    const cell = document.createElement("span");
    cell.dataset.kind = segment.kind;
    cell.dataset.shared = segment.shared;
    cell.title = `Segment ${segment.index} · ${segment.active} in current root · ${segment.alternate} in other root`;
    return cell;
  }));
  const kinds = new Set(map.segments.map((segment) => segment.kind));
  const legend = node("flash-map-legend");
  legend.replaceChildren(...Object.entries(labels).filter(([kind]) => kinds.has(kind)).map(([kind, label]) => {
    const item = document.createElement("li");
    const marker = document.createElement("span");
    marker.dataset.kind = kind;
    item.append(marker, label);
    return item;
  }));
  if (map.segments.some((segment) => segment.shared)) {
    const item = document.createElement("li");
    const marker = document.createElement("span");
    marker.dataset.shared = true;
    item.append(marker, "Referenced by both roots");
    legend.append(item);
  }
  node("flash-map-result").hidden = false;
  notify("Catalog layout loaded.");
}
function updateImportControls() {
  const named = node("import").elements.name.value.trim() !== "";
  node("import-preview").textContent = named
    ? (state?.adaptive_imports ? "Preview new dictionary" : "Import new dictionary")
    : "Import into personal";
  node("import-help").textContent = named && state?.adaptive_imports
    ? "Preview checks how much space the new dictionary needs. Nothing is written until you approve."
    : "Enter a name to create a separate dictionary, or leave it blank to import into your personal dictionary.";
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
}
function showImportPlan(plan) {
  node("import-plan-summary").textContent = `${plan.rationale} ${plan.free_segments} free segments, ${plan.largest_free_run} contiguous, ${plan.reserve} reserved.`;
  const costs = node("import-plan-costs");
  costs.replaceChildren();
  for (const [format, option] of Object.entries(plan.options)) {
    const row = document.createElement("li");
    const size = option.logical_bytes === null ? "" : ` ${number.format(option.logical_bytes)} B.`;
    const staging = option.staging_segments === undefined ? "" : ` ${option.added_segments} added and ${option.staging_segments} staging segments.`;
    const replacement = option.replacement_staging_segments === undefined ? "" :
      ` A same-size replacement fits with ${option.replacement_staging_segments} staging segments.`;
    const refusal = option.eligible ? "" : ` ${option.reason}`;
    row.textContent = `${format}.${size}${staging}${replacement}${refusal}`;
    costs.append(row);
  }
  node("import-apply").hidden = !plan.selected || !importToApply;
  node("import-apply").textContent = `Stage ${plan.selected || ""} import`;
  node("import-plan").hidden = false;
  if (importToApply) importToApply.token = plan.token;
  notify(plan.selected ? "Review the staging plan before importing." : "Import stopped.");
  if (!plan.selected) showImportError("This format cannot stage safely.");
}
function clearOptimizationPlan() {
  optimizationToApply = undefined;
  node("optimization-plan").hidden = true;
}
function showOptimizationPlan(plan) {
  if (optimizationToApply?.id !== plan.id || optimizationToApply.operation !== plan.operation) return;
  optimizationToApply.token = plan.token;
  node("optimization-summary").textContent =
    `${plan.name} · ${number.format(plan.compact_bytes)} B · ${plan.image_segments} image segments · ${plan.staging_segments} staging segments. ` +
    `${plan.free_segments} free, ${plan.largest_free_run} contiguous, ${plan.reserve} held in reserve.`;
  const effects = {
    personal: "Convert the existing personal entries to a compact baseline and retain existing tombstones.",
    consolidate: "Fold the current edits and removals into the compact baseline.",
    replace: "Replace the baseline from this file. Saved edits and tombstones still take precedence."
  };
  node("optimization-content").textContent = effects[plan.operation];
  node("optimization-plan").hidden = false;
  notify("Review the staging cost before approving the change.");
}
async function run(payload) {
  if (busy) return;
  const importing = ["import", "preview_import", "apply_import"].includes(payload.action);
  if (importing) showImportError();
  if (payload.action !== "preview_optimize") clearOptimizationPlan();
  busy = true;
  controls();
  notify("Connecting to the keyboard…");
  try {
    const {id} = await request("/api/job", payload);
    await watch(id);
  } catch (error) {
    if (importing) {
      notify("Import stopped.");
      showImportError(error.message);
    } else {
      notify(error.message, true);
    }
  } finally {
    busy = false;
    controls();
  }
}
for (const [id, action] of [["refresh", "refresh"], ["backup", "backup"], ["updates", "check_updates"], ["measure", "measure"]]) {
  node(id).onclick = () => run({action});
}
node("compact").onclick = () => {
  if (window.confirm("Compact the journal over USB? Typing may pause and the left half may reboot.")) {
    run({action: "compact"});
  }
};
node("backup-complete").onclick = () => run({action: "backup", complete: true});
node("optimize").onclick = () => {
  const row = selection();
  clearOptimizationPlan();
  optimizationToApply = {id: row.id, operation: row.kind === "personal" ? "personal" : "consolidate"};
  run({action: "preview_optimize", ...optimizationToApply});
};
node("replace-snapshot").onchange = clearOptimizationPlan;
node("replace-snapshot").onsubmit = async (event) => {
  event.preventDefault();
  const row = selection();
  clearOptimizationPlan();
  try {
    const source = await new FormData(event.currentTarget).get("file").text();
    if (selection()?.id !== row.id || busy) return;
    optimizationToApply = {id: row.id, operation: "replace", source};
    await run({action: "preview_optimize", ...optimizationToApply});
  } catch (error) {
    notify(error.message, true);
  }
};
node("optimization-apply").onclick = () => {
  if (optimizationToApply?.token) run({action: "apply_optimize", ...optimizationToApply});
};
node("optimization-cancel").onclick = clearOptimizationPlan;
node("flash-map-scan").onclick = () => run({action: "layout_map"});
node("apply-updates").onclick = () => {
  if (window.confirm("Apply the pinned base revisions? The left half will reboot after saving.")) {
    run({action: "apply_updates", approved: true});
  }
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
  const name = selection().name;
  const confirm = window.prompt(`Type ${name} to delete it and its entries from the active catalog.`);
  if (confirm === name) run({action: "delete", id: selectedId, confirm});
};
node("edit").onsubmit = (event) => {
  event.preventDefault();
  const fields = new FormData(event.currentTarget);
  run({action: event.submitter.value, id: selectedId, outline: fields.get("outline").trim(), translation: fields.get("translation")});
};
node("import").oninput = () => {
  showImportError();
  clearImportPlan();
  updateImportControls();
};
node("import-apply").onclick = () => {
  if (importToApply?.token) {
    const approved = importToApply;
    clearImportPlan();
    run({action: "apply_import", ...approved});
  }
};
for (const action of ["import", "restore"]) {
  node(action).onsubmit = async (event) => {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    try {
      const source = await fields.get("file").text();
      const options = action === "import"
        ? {name: fields.get("name").trim()}
        : {confirm: fields.get("confirm")};
      if (action === "import" && state?.adaptive_imports && options.name) {
        clearImportPlan();
        importToApply = {source, ...options, preference: fields.get("preference") ?? "auto"};
        await run({action: "preview_import", ...importToApply});
      } else {
        if (action === "import" && !options.name && !window.confirm(
          "Import this file into your personal dictionary? Entries with matching outlines will be replaced."
        )) return;
        await run({action, source, ...options});
      }
    } catch (error) {
      if (action === "import") showImportError(error.message);
      else notify(error.message, true);
    }
  };
}

controls();
request("/api/job").then(async (job) => {
  if (job.status !== "running") return;
  busy = true;
  controls();
  try {
    await watch(job.id);
  } catch (error) {
    notify(error.message, true);
  } finally {
    busy = false;
    controls();
  }
}).catch((error) => notify(error.message, true));
