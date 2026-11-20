const node = (id) => document.getElementById(id);
const status = node("status");
const token = document.querySelector('[name="manager-token"]').content;
let state;
let selectedId;
let busy = false;
let importToApply;
let optimizationToApply;
let cleanupToken;
const number = new Intl.NumberFormat();
const jobMessages = {
  measure: "Measuring saved edits…", preview_optimize: "Calculating compact storage…",
  apply_optimize: "Saving compact dictionary…", layout_map: "Reading flash layout…",
  preview_import: "Calculating import size…", apply_import: "Importing dictionary…", import: "Importing dictionary…",
  backup: "Preparing backup…", export: "Exporting dictionary…", restore: "Restoring backup…",
  preview_compact: "Checking journal space…", compact: "Compacting journal…", clear_personal: "Clearing personal dictionary…"
};

function notify(text, error = false) {
  status.textContent = text;
  status.dataset.error = error;
}
function selection() {
  return state?.catalog.find((row) => row.id === selectedId);
}
function controls() {
  node("job-status").toggleAttribute("data-busy", busy);
  node("job-status").querySelector(".job-spinner").hidden = !busy;
  for (const el of document.querySelectorAll("[data-write]")) el.disabled = busy || !state || state.read_only;
  for (const id of ["refresh", "backup", "backup-complete", "updates", "export", "restore-submit", "measure"]) node(id).disabled = busy;
  node("flash-map-scan").disabled = busy;
  for (const radio of node("import-plan-costs").querySelectorAll("input")) {
    radio.disabled = busy || !!state?.read_only || radio.dataset.eligible !== "true";
  }
  if (!state) return;
  const row = selection();
  const rank = state.catalog.indexOf(row);
  const editable = !busy && !state.read_only;
  node("up").disabled = !editable || rank <= 0;
  node("down").disabled = !editable || rank < 0 || rank >= state.catalog.length - 1;
  const independent = row?.name !== "user_dictionary" && ["named", "snapshot"].includes(row?.kind);
  const builtIn = row?.name === "user_dictionary";
  node("rename").disabled = !editable || !independent;
  const canClearPersonal = builtIn && (row.kind === "personal" || state.personal_clear);
  node("delete").disabled = !editable || !(independent || canClearPersonal);
  const snapshot = state.adaptive_imports && row?.kind === "snapshot";
  const personal = state.personal_conversion && row?.kind === "personal";
  node("optimize").hidden = !snapshot && !personal;
  node("optimize").disabled = !editable;
  node("export").disabled = busy || !row;
  node("target").textContent = row ? `Editing ${row.name}` : "Select a dictionary first.";
  const canRevert = row?.kind === "base";
  node("edit-note").hidden = !row;
  node("edit-revert-note").hidden = !canRevert;
  for (const el of node("edit").querySelectorAll("button")) {
    const unavailable = el.value === "reset" && !canRevert;
    el.hidden = unavailable;
    el.disabled = !editable || !row || unavailable;
  }
}
function render(newState, usage = {}) {
  state = newState;
  clearImportPlan();
  clearOptimizationPlan();
  node("flash-map-panel").hidden = !state.layout_map;
  node("flash-map-error").hidden = true;
  node("flash-map-result").hidden = true;
  updateImportControls();
  if (!selection()) selectedId = state.catalog[0]?.id;
  node("catalog").replaceChildren();
  const sizes = new Map((state.images ?? []).flatMap((image) => image.dictionaries.map((row) => [row.id, row.bytes])));
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
    const image = tr.insertCell();
    const bytes = sizes.get(row.id) ?? (row.kind === "snapshot" ? row.image_bytes : null);
    image.textContent = bytes == null ? "—" : formatBytes(bytes);
    if (bytes != null) image.title = `${number.format(bytes)} bytes`;
    const saved = usage[row.id];
    tr.insertCell().textContent = saved ? `${number.format(saved.count)} · ${number.format(saved.bytes)} B` : "—";
  }
  node("layout").replaceChildren(...state.layouts.map((layout) => new Option(layout, layout)));
  node("layout").value = state.layout;
  node("refresh").textContent = "Refresh connection";
  node("workspace").hidden = false;
  node("backup-complete").hidden = !state.adaptive_imports;
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
async function watch(id) {
  let job;
  do {
    await new Promise((resolve) => setTimeout(resolve, 550));
    job = await request("/api/job");
    if (job.id !== id) throw new Error("Operation status changed. Refresh the keyboard to check its saved state.");
    if (job.message && job.message !== "Connecting over USB HID…" && job.status === "running") notify(job.message);
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
    } else if (result.cleared) {
      notify("Personal dictionary cleared.");
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
  } else if (result.cleanup) {
    const {token, reclaimable} = result.cleanup;
    cleanupToken = token;
    if (result.layout_map) showFlashMap(result.layout_map, reclaimable);
    node("journal-summary").textContent = reclaimable.length
      ? `${number.format(reclaimable.length)} journal blocks can be released. Applying cleanup may pause typing and reboot the left half.`
      : "No journal blocks can be reclaimed in this pass.";
    node("compact-apply").hidden = !token;
    node("journal-preview").hidden = false;
    notify(token ? "Review journal cleanup." : "Journal cleanup complete. No blocks to reclaim.");
  } else if (result.layout_map) {
    showFlashMap(result.layout_map);
  }
}
function formatBytes(bytes) {
  return bytes < 1024 ? `${number.format(bytes)} B` : `${number.format(Math.round(bytes / 102.4) / 10)} KiB`;
}
function showFlashMap(map, reclaimable = []) {
  node("flash-map-error").hidden = true;
  const summary = node("flash-map-summary");
  summary.textContent = map.free_segments === null
    ? "Free space is unknown. A catalog is unavailable or the store is in read-only recovery."
    : map.staging_blocked ? "A retained catalog reader currently blocks publication." : "";
  summary.hidden = !summary.textContent;
  const grid = node("flash-map-grid");
  const labels = {c: "Catalog roots", x: "Reserved", b: "Unidentified pinned image", s: "Unidentified user snapshot", h: "Latest journal",
    j: "Journals", f: "Forward index", r: "Reverse index", "retained-journal": "Retained-only journals",
    retained: "Retained-only extents", ".": "Unreferenced", "?": "Unknown"};
  const palette = {b: ["#478fb9", "#658ce1", "#55a9c3"], s: ["#5cd1a3", "#7cac62", "#2db6a5"]};
  const nextColor = {b: 0, s: 0};
  const bySegment = new Map();
  const candidates = new Set(reclaimable);
  const images = (map.images ?? []).filter((image) =>
    map.segments.slice(image.start, image.start + image.segments).every((segment) => segment.kind === image.kind)
  );
  for (const image of images) {
    const names = image.dictionaries.map((row) => row.name);
    image.label = names.includes("Lapwing Base") ? "Lapwing Base" : names[0];
    image.color = palette[image.kind][nextColor[image.kind]++ % palette[image.kind].length];
    for (let i = image.start; i < image.start + image.segments; i++) bySegment.set(i, image);
  }
  grid.replaceChildren(...map.segments.map((segment) => {
    const cell = document.createElement("span");
    cell.dataset.kind = segment.kind;
    const image = bySegment.get(segment.index);
    cell.title = `Segment ${segment.index} · ${image?.label ?? labels[segment.kind]}`;
    if (segment.kind === "j" && candidates.has(segment.index)) {
      cell.dataset.reclaimable = "true";
      cell.title += " · Can be reclaimed";
    }
    if (image) cell.style.backgroundColor = image.color;
    return cell;
  }));
  const kinds = new Set(map.segments.filter((segment) => !bySegment.has(segment.index)).map((segment) => segment.kind));
  const legend = node("flash-map-legend");
  legend.replaceChildren(...Object.entries(labels).filter(([kind]) => kinds.has(kind)).map(([kind, label]) => {
    const item = document.createElement("li");
    const marker = document.createElement("span");
    marker.dataset.kind = kind;
    item.append(marker, label);
    return item;
  }));
  for (const image of images) {
    const item = document.createElement("li");
    const marker = document.createElement("span");
    marker.style.backgroundColor = image.color;
    item.append(marker, image.label);
    legend.append(item);
  }
  if (grid.querySelector("[data-reclaimable]")) {
    const item = document.createElement("li");
    const marker = document.createElement("span");
    marker.dataset.reclaimable = "true";
    item.append(marker, "Can be reclaimed");
    legend.append(item);
  }
  node("flash-map-result").hidden = false;
  notify("Catalog layout loaded.");
}
function updateImportControls() {
  const fields = node("import").elements;
  const named = fields.name.value.trim() !== "";
  node("import-preview").textContent = named
    ? (state?.adaptive_imports ? "Preview new dictionary" : "Import new dictionary")
    : "Import into personal";
  node("import-help").hidden = !(named && state?.adaptive_imports);
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
function selectImportFormat(plan, format, manual = false) {
  const token = plan.tokens[format];
  if (manual && (busy || !importToApply || !token)) return;
  for (const row of node("import-plan-costs").children) {
    const selected = row.dataset.format === format;
    row.toggleAttribute("data-selected", selected);
    row.querySelector("input").checked = selected;
  }
  const apply = node("import-apply");
  apply.hidden = !importToApply || !token;
  apply.textContent = format === "snapshot" ? "Import compact snapshot" : "Import as journal";
  if (importToApply) {
    importToApply.preference = manual ? format : "auto";
    importToApply.token = token;
  }
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
  const image = document.createElement("span");
  image.className = "caption-added";
  image.textContent = `${imageSegments} ${imageLabel}`;
  const staging = document.createElement("span");
  staging.className = "caption-staging";
  staging.textContent = `${stagingSegments} to stage`;
  staging.title = "Includes the solid and striped segments";
  caption.append(image, staging);
  return [blocks, caption];
}

function showImportPlan(plan) {
  if (!importToApply) {
    notify("Preview the current dictionary file.");
    return;
  }
  node("import-entry-count").textContent = `${number.format(plan.entries)} ${plan.entries === 1 ? "entry" : "entries"} to import`;

  const costs = node("import-plan-costs");
  costs.replaceChildren();
  for (const [format, option] of Object.entries(plan.options)) {
    const row = document.createElement("li");
    row.className = "import-format";
    row.dataset.format = format;

    const header = document.createElement("div");
    header.className = "import-format-heading";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "import-format";
    radio.id = `import-format-${format}`;
    radio.dataset.eligible = String(option.eligible);
    radio.onchange = () => selectImportFormat(plan, format, true);
    const name = document.createElement("label");
    name.htmlFor = radio.id;
    name.textContent = format === "snapshot" ? "Compact snapshot" : "Journal";
    row.onclick = (event) => {
      if (event.target.closest("input, label") || radio.disabled) return;
      radio.focus({preventScroll: true});
      selectImportFormat(plan, format, true);
    };
    header.append(radio, name);
    if (format === plan.recommended) {
      const label = document.createElement("span");
      label.className = "import-choice";
      label.textContent = "Recommended";
      header.append(label);
    }
    if (option.logical_bytes != null) {
      const size = document.createElement("span");
      size.className = "import-bytes";
      size.textContent = option.logical_bytes < 1024
        ? `${number.format(option.logical_bytes)} B`
        : `${number.format(Math.round(option.logical_bytes / 1024))} KiB`;
      size.title = `${number.format(option.logical_bytes)} bytes`;
      header.append(size);
    }
    row.append(header);

    if (option.staging_segments != null) {
      row.append(...segmentCost(option.added_segments, option.staging_segments, "added"));
    }

    if (option.consolidation_staging_segments != null || option.reason) {
      const detail = document.createElement("p");
      detail.className = "import-format-detail";
      detail.textContent = option.eligible
        ? `Same-size consolidation fits · ${option.consolidation_staging_segments} to stage`
        : option.reason;
      row.append(detail);
    }
    costs.append(row);
  }

  const savings = node("import-savings");
  const saved = plan.options.journal.added_segments - plan.options.snapshot.added_segments;
  savings.hidden = !(saved > 0 && plan.options.journal.eligible && plan.options.snapshot.eligible);
  savings.textContent = savings.hidden ? "" : `${saved} fewer segments with a snapshot`;
  selectImportFormat(plan, plan.selected);
  node("import-plan").hidden = false;
  node("import-preview").hidden = true;
  notify(plan.selected ? "Review the import preview." : "Import stopped.");
  if (!plan.selected) showImportError("This format cannot stage safely. USB rebuilding is unavailable in Manager.");
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
  node("optimization-allocation").textContent = plan.token ? `${allocation} · ${plan.staging_segments} to stage` : "No saved edits to consolidate.";
  node("optimization-apply").hidden = !plan.token;
  node("optimization-publish").hidden = !plan.token;
  node("optimization-plan").hidden = false;
  notify(plan.token ? "Review before applying consolidation." : "No saved edits to consolidate.");
}
async function run(payload) {
  if (busy) return;
  const importing = ["import", "preview_import", "apply_import"].includes(payload.action);
  let refreshMap = false;
  if (importing) showImportError();
  if (payload.action !== "preview_optimize") clearOptimizationPlan();
  cleanupToken = undefined;
  node("journal-preview").hidden = true;
  node("flash-map-legend").querySelector("[data-reclaimable]")?.parentElement.remove();
  for (const cell of node("flash-map-grid").querySelectorAll("[data-reclaimable]")) {
    delete cell.dataset.reclaimable;
    cell.title = cell.title.replace(" · Can be reclaimed", "");
  }
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
    refreshMap = !existing && ["import", "apply_import", "compact"].includes(payload.action);
  } catch (error) {
    if (importing) {
      notify("Import stopped.");
      showImportError(error.message);
    } else if (payload.action === "layout_map") {
      const failure = node("flash-map-error");
      failure.textContent = /scan deferred/i.test(error.message)
        ? "Pause typing and wait for any storage operation to finish, then scan again."
        : error.message.replace(/^ERR\s+/, "");
      failure.hidden = false;
      notify("Layout scan stopped.", true);
      failure.focus();
    } else {
      notify(error.message, true);
    }
  } finally {
    busy = false;
    controls();
  }
  if (refreshMap && state?.layout_map) {
    await scanLayout();
    if (!node("flash-map-result").hidden && node("flash-map-error").hidden) {
      node("flash-map-panel").scrollIntoView({block: "nearest"});
    }
  }
}
for (const [id, action] of [["refresh", "refresh"], ["backup", "backup"], ["updates", "check_updates"], ["measure", "measure"]]) {
  node(id).onclick = () => run({action});
}
node("compact").onclick = () => run({action: "preview_compact"});
node("compact-apply").onclick = () => {
  if (cleanupToken) run({action: "compact", token: cleanupToken});
};
node("backup-complete").onclick = () => run({action: "backup", complete: true});
node("optimize").onclick = () => {
  const row = selection();
  clearOptimizationPlan();
  optimizationToApply = {id: row.id, operation: row.kind === "personal" ? "personal" : "consolidate"};
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
node("apply-updates").onclick = () => {
  if (window.confirm("Download a backup before updating the pinned dictionaries. Apply the pinned revisions now? The left half will reboot after saving.")) {
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
  if (event.target.name === "file" && event.target.files.length) {
    const stem = event.target.files[0].name.replace(/\.json$/i, "").normalize("NFKD").replace(/\p{M}/gu, "");
    node("import").elements.name.value = stem.replace(/[^A-Za-z0-9 ._-]+/g, "-")
      .replace(/^[^A-Za-z0-9]+/, "").slice(0, 44).trimEnd() || "Imported dictionary";
  }
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
    if (busy || (action === "import" && node("import-preview").hidden)) return;
    const fields = new FormData(event.currentTarget);
    try {
      const source = await fields.get("file").text();
      const options = action === "import"
        ? {name: fields.get("name").trim()}
        : {confirm: fields.get("confirm")};
      if (action === "import" && state?.adaptive_imports && options.name) {
        clearImportPlan();
        importToApply = {source, ...options};
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
  if (busy || job.status !== "running") return;
  busy = true;
  controls();
  notify(job.message || "Working on the keyboard…");
  try {
    await watch(job.id);
  } catch (error) {
    notify(error.message, true);
  } finally {
    busy = false;
    controls();
  }
}).catch((error) => notify(error.message, true));
