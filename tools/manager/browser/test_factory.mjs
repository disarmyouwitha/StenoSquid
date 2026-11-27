// node tools/manager/browser/test_factory.mjs   (no browser required)
// The bundled factory dictionaries: the zip reader and the lookup that stands in for manager_editor.factory_source. The archive is
// built here with node:zlib so stored and deflated entries, a missing name, a damaged archive and the digest rules are all covered.
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {deflateRawSync, crc32 as nodeCrc} from "node:zlib";
import {readZipEntry, makeFactorySource} from "./factory.js";

function zipOf(files) {  // files: [[name, Buffer, "stored"|"deflate"]]
  const parts = [], central = [];
  let offset = 0;
  for (const [name, data, mode] of files) {
    const body = mode === "stored" ? data : deflateRawSync(data);
    const nameBytes = Buffer.from(name);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt16LE(mode === "stored" ? 0 : 8, 8);
    local.writeUInt32LE(nodeCrc(data), 14); local.writeUInt32LE(body.length, 18); local.writeUInt32LE(data.length, 22); local.writeUInt16LE(nameBytes.length, 26);
    const head = Buffer.alloc(46);
    head.writeUInt32LE(0x02014b50, 0); head.writeUInt16LE(20, 4); head.writeUInt16LE(20, 6); head.writeUInt16LE(mode === "stored" ? 0 : 8, 10);
    head.writeUInt32LE(nodeCrc(data), 16); head.writeUInt32LE(body.length, 20); head.writeUInt32LE(data.length, 24); head.writeUInt16LE(nameBytes.length, 28);
    head.writeUInt32LE(offset, 42);
    central.push(head, nameBytes);
    parts.push(local, nameBytes, body);
    offset += 30 + nameBytes.length + body.length;
  }
  const directory = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(files.length, 8); end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16);
  return new Uint8Array(Buffer.concat([...parts, directory, end]));
}
const sha = (buffer) => createHash("sha256").update(buffer).digest("hex");

const base = Buffer.from(JSON.stringify({KAT: "cat", "TKOG": "dog", "1-": "one", "STKPWHRAOEUFRPBLGTSDZ": "all", "KAT/TKOG": "cat dog", "STK": "é日本"}));
const commands = Buffer.from('{"A": "a"}');
const zip = zipOf([["build/lapwing/lapwing-base.json", base, "deflate"], ["build/extras/lapwing-commands.json", commands, "stored"],
  ["build/other/ignored.json", Buffer.from("{}"), "deflate"]]);

// the reader
assert.deepEqual(Buffer.from(await readZipEntry(zip, "build/lapwing/lapwing-base.json")), base);
assert.deepEqual(Buffer.from(await readZipEntry(zip, "build/extras/lapwing-commands.json")), commands);
assert.equal(await readZipEntry(zip, "build/plover/plover-main.json"), null);
assert.equal(await readZipEntry(zip, "build/lapwing"), null);
await assert.rejects(() => readZipEntry(new Uint8Array(100), "x"), /damaged/);
const sliced = new Uint8Array(zip.buffer.slice(0)).subarray(0);  // an offset view must work too
const padded = new Uint8Array(zip.length + 10); padded.set(zip, 10);
assert.deepEqual(Buffer.from(await readZipEntry(padded.subarray(10), "build/lapwing/lapwing-base.json")), base);

// the lookup
let opened = 0;
const source = makeFactorySource(() => { opened++; return zip; });
const got = await source(6, "Lapwing Base", sha(base));
assert.ok(got instanceof Map);
assert.equal(got.size, 6);
assert.equal(got.get("KAT"), "cat");
assert.equal(got.get("KAT/TKOG"), "cat dog");
assert.equal(got.get("STK"), "é日本");
assert.equal(got.get("#S"), "one", "1- is the number-bar S");
assert.deepEqual([...got.keys()].slice(0, 2), ["KAT", "TKOG"], "file order is kept");
// each caller gets its own Map
got.set("X", "y");
assert.equal((await source(6, "Lapwing Base", sha(base))).has("X"), false);
assert.equal(opened, 1, "the archive is read once for repeated lookups");
// rules from manager_editor.factory_source
assert.equal(await source(6, "Lapwing Base", sha(Buffer.from("different"))), null, "a digest that differs");
assert.equal(await source(6, "Lapwing Base", ""), null, "no digest");
assert.equal(await source(6, "Lapwing Base", null), null, "no digest (null)");
assert.equal(await source(6, "Plover Base", sha(base)), null, "the name does not belong to that identity");
assert.equal(await source(99, "Lapwing Base", sha(base)), null, "an unknown identity");
assert.equal(await source(7, "Plover Base", sha(base)), null, "a file that is not in the bundle");
assert.equal((await source(2, "Lapwing Commands", sha(commands))).get("A"), "a");
assert.equal(await makeFactorySource(() => null)(6, "Lapwing Base", sha(base)), null, "no bundle at all");
// only the last six lookups are remembered
let reads = 0;
const small = makeFactorySource(() => { reads++; return zip; });
for (const [identity, name, file] of [[6, "Lapwing Base", base], [2, "Lapwing Commands", commands]]) await small(identity, name, sha(file));
await small(6, "Lapwing Base", sha(base));
assert.equal(reads, 2);
console.log("PASS bundled factory dictionaries (JS): zip reader and factory_source rules");
