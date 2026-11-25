// Run tools/bench/manager_workloads.py inside Pyodide (the engine the browser Manager ships).
//   node --experimental-wasm-jspi tools/bench/manager_workloads.mjs REPO_ROOT PYODIDE_DIR
import { loadPyodide } from "PYODIDE_PKG";
import path from "node:path";
const [root, pyodideDir] = [path.resolve(process.argv[2] ?? "."), process.argv[3]];
const t0 = performance.now();
const py = await loadPyodide({ indexURL: pyodideDir });
const startup = performance.now() - t0;
py.FS.mkdirTree("/app");
py.FS.mount(py.FS.filesystems.NODEFS, { root }, "/app");
const t1 = performance.now();
const result = JSON.parse(await py.runPythonAsync(`
import sys
sys.path.insert(0, "/app/tools/bench")
import manager_workloads, json
json.dumps(manager_workloads.run("/app"))
`));
result.pyodide_startup_ms = Math.round(startup);
result.total_workload_ms = Math.round(performance.now() - t1);
console.log(JSON.stringify(result));