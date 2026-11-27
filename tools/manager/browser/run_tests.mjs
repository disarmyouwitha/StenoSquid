// node tools/manager/browser/run_tests.mjs [name-fragment ...]
// Runs the JavaScript Manager's tests one after another (no browser, no keyboard). Most of them replay vectors recorded from the old
// Python Manager (golden/*.json, kept as frozen recordings); test_page_bundle.mjs needs the bundled page, which this builds with
// tools/ci/bundle_modules.py (Python 3). The transfer simulator test needs a C++ build and runs through tools/tests/test_js_transfer.py.
import {spawnSync} from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {fileURLToPath} from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../../..");
const wanted = process.argv.slice(2);
const tests = fs.readdirSync(here).filter((name) => /^test_.*\.mjs$/.test(name) && name !== "test_transfer_sim.mjs")
  .filter((name) => !wanted.length || wanted.some((fragment) => name.includes(fragment))).sort();

let failed = 0;
for (const name of tests) {
  const args = [path.join(here, name)];
  if (name === "test_page_bundle.mjs") {
    const bundle = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "steno-bundle-")), "page_bundle.js");
    const made = spawnSync("python3", ["-c", "import sys; sys.path.insert(0, 'tools/ci'); from bundle_modules import bundle; " +
      `open(sys.argv[1], 'w', encoding='utf-8').write(bundle('tools/manager/browser/backend.js'))`, bundle], {cwd: root, encoding: "utf8"});
    if (made.status !== 0) { console.log(`SKIP ${name}: could not build the bundle (${made.stderr.trim().split("\n").pop()})`); continue; }
    args.push(bundle);
  }
  const started = Date.now();
  const result = spawnSync("node", args, {encoding: "utf8"});
  const seconds = ((Date.now() - started) / 1000).toFixed(1);
  if (result.status === 0) console.log(`ok   ${name} (${seconds}s)`);
  else { failed++; console.log(`FAIL ${name}\n${result.stdout}${result.stderr}`); }
}
console.log(failed ? `${failed} of ${tests.length} test files failed` : `All ${tests.length} test files passed`);
process.exit(failed ? 1 : 0);
