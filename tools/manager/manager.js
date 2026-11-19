const node = (id) => document.getElementById(id);
const status = node("status");
const token = document.querySelector('[name="manager-token"]').content;
let state;
let selectedId;
let busy = false;
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
  for (const id of ["refresh", "backup", "updates", "export"]) node(id).disabled = busy;
  if (!state) return;
  const row = selection();
  const rank = state.catalog.indexOf(row);
  const editable = !busy && !state.read_only;
  node("up").disabled = !editable || rank <= 0;
  node("down").disabled = !editable || rank < 0 || rank >= state.catalog.length - 1;
  for (const id of ["rename", "delete"]) node(id).disabled = !editable || row?.kind !== "named";
  node("export").disabled = busy || !row;
  node("target").textContent = row ? `Editing ${row.name}` : "Select a dictionary first.";
  for (const el of node("edit").querySelectorAll("button")) {
    el.disabled = !editable || !row || (el.value === "reset" && row.kind !== "base");
  }
}
function render(newState) {
  state = newState;
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
    radio.onchange = () => { selectedId = row.id; controls(); };
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
    tr.insertCell().textContent = row.kind === "base" ? number.format(row.base_entries) : "—";
    tr.insertCell().textContent = row.image_bytes ? `${number.format(Math.ceil(row.image_bytes / 1024))} KiB` : "—";
  }
  node("layout").replaceChildren(...state.layouts.map((layout) => new Option(layout, layout)));
  node("layout").value = state.layout;
  node("refresh").textContent = "Refresh connection";
  node("workspace").hidden = false;
  node("maintenance").textContent = `${state.tail} pending index edits · ${state.auto_checkpoints} automatic checkpoints this boot. Compiled images may be shared by several dictionaries.`;
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
    if (job.message) notify(job.message, job.status === "error");
  } while (job.status === "running");
  if (job.status === "error") throw new Error(job.message);
  const result = job.result;
  if (result.state) {
    render(result.state);
    notify(state.read_only ? "Connected. The keyboard is in read-only recovery." : "Keyboard ready.");
  } else if (result.download) {
    download(result.filename, result.download);
    notify(`Downloaded ${result.filename}${result.count === undefined ? "" : ` with ${number.format(result.count)} entries`}.`);
  } else if (result.updates) {
    notify(result.updates.length
      ? `Pinned updates available for ${result.updates.join(", ")}. A safe in-place update is unavailable in this build.`
      : "All installed bases match this version’s pinned sources.");
  }
}
async function run(payload) {
  if (busy) return;
  busy = true;
  controls();
  notify("Connecting to the keyboard…");
  try {
    const {id} = await request("/api/job", payload);
    await watch(id);
  } catch (error) {
    notify(error.message, true);
  } finally {
    busy = false;
    controls();
  }
}
for (const [id, action] of [["refresh", "refresh"], ["backup", "backup"], ["updates", "check_updates"], ["compact", "compact"]]) {
  node(id).onclick = () => run({action});
}
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
node("import").onsubmit = async (event) => {
  event.preventDefault();
  const fields = new FormData(event.currentTarget);
  try {
    await run({action: "import", source: await fields.get("file").text(), name: fields.get("name").trim()});
  } catch (error) {
    notify(error.message, true);
  }
};
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
