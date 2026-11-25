// node compare_compile.mjs FOLDER  (driven by compare_compile.py)
import fs from "node:fs";
import path from "node:path";
import { compile, readFlatJson } from "./compile_dict.mjs";
import { compileFast } from "./compile_fast.mjs";

const folder = process.argv[2];
const cases = JSON.parse(fs.readFileSync(path.join(folder, "cases.json"), "utf8"));
const same = (name, image, reference) => {
  if (image.length !== reference.length) { console.error(`${name}: length ${image.length} != ${reference.length}`); return false; }
  for (let i = 0; i < image.length; i++) if (image[i] !== reference[i]) { console.error(`${name}: first difference at byte ${i}`); return false; }
  return true;
};
for (const { name, base, inputs } of cases) {
  const reference = fs.readFileSync(path.join(folder, `${name}.ref`));
  const raws = inputs.map(({ label, path: file }) => ({ name: label, json: fs.readFileSync(file) }));
  let start = performance.now();
  const naive = compile(raws.map(({ name: n, json }) => { const [keys, values] = readFlatJson(json); return { name: n, keys, values }; }), base);
  const naiveMs = performance.now() - start;
  start = performance.now();
  const fast = compileFast(raws, base);
  const fastMs = performance.now() - start;
  start = performance.now();
  compileFast(raws, base); // second run in the same process: what a long-lived tab pays after the first import
  const warmMs = performance.now() - start;
  console.log(JSON.stringify({
    name, bytes: fast.length, identical: same(name + " (naive)", naive, reference) && same(name + " (fast)", fast, reference),
    js_ms: Math.round(fastMs), naive_js_ms: Math.round(naiveMs), warm_js_ms: Math.round(warmMs),
  }));
}