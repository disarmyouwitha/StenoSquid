import fs from "node:fs";
import { compileFast } from "./compile_fast.mjs";
const raw = fs.readFileSync(process.argv[2]);
for (let run = 1; run <= 4; run++) { const s = performance.now(); compileFast([{ name: "x", json: raw }], 0); console.log(`run ${run}: ${(performance.now() - s).toFixed(0)} ms`); }