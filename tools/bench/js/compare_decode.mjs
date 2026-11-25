// node compare_decode.mjs FOLDER (driven by compare_decode.py)
import fs from "node:fs";
import path from "node:path";
import { compactEntries } from "./decode_dict.mjs";

const folder = process.argv[2];
for (const { name } of JSON.parse(fs.readFileSync(path.join(folder, "cases.json"), "utf8"))) {
  const blob = new Uint8Array(fs.readFileSync(path.join(folder, `${name}.bin`)));
  const reference = JSON.parse(fs.readFileSync(path.join(folder, `${name}.json`), "utf8"));
  const start = performance.now();
  const entries = compactEntries(blob, 0, 0);
  const jsMs = performance.now() - start;
  let identical = entries.length === reference.length;
  for (let i = 0; identical && i < entries.length; i++) identical = entries[i][0] === reference[i][0] && entries[i][1] === reference[i][1];
  console.log(JSON.stringify({ name, identical, js_ms: Math.round(jsMs * 10) / 10 }));
}
