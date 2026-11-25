// node tools/manager/browser/test_transfer_frame.mjs   (no browser required)
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import zlib from "node:zlib";
import {encode, Decoder, crc32, Type, MAX_PAYLOAD} from "./transfer_frame.js";

// Same golden frame the C++ test uses, built independently in Python with zlib.
const golden = "b5020201100000000300" + "414243";
const expected = Buffer.from(golden, "hex");
const body = Uint8Array.from(expected);
const frame = encode(Type.DATA, 0x0102, 0x10, Uint8Array.from([0x41, 0x42, 0x43]));
assert.equal(Buffer.from(frame).toString("hex"), golden + "32937f59"); // CRC literal from Python zlib
assert.deepEqual(Array.from(frame.subarray(0, 13)), Array.from(body));
assert.equal(crc32(Buffer.from("123456789")), 0xcbf43926);
assert.equal(crc32(frame.subarray(0, 13)), new DataView(frame.buffer).getUint32(13, true));
assert.equal(crc32(frame.subarray(0, 13)), zlib.crc32 ? zlib.crc32(Buffer.from(frame.subarray(0, 13))) : crc32(frame.subarray(0, 13)));

// Byte-by-byte delivery with padding, embedded magic bytes and a corrupt frame between.
const a = encode(Type.DATA, 7, 0, Uint8Array.from([0xb5, 0xb5, 1, 2]));
const bad = encode(Type.DATA, 7, 4, Uint8Array.from([9, 9])); bad[10] ^= 0x40;
const b = encode(Type.END, 7, 4);
const decoder = new Decoder();
const seen = [];
for (const byte of [...a, 0, 0, 0, ...bad, ...b]) { decoder.push(Uint8Array.of(byte)); seen.push(...decoder.frames()); }
assert.deepEqual(seen.map((f) => f.type), [Type.DATA, Type.END]);
assert.deepEqual(Array.from(seen[0].payload), [0xb5, 0xb5, 1, 2]);
assert.ok(decoder.crcErrors >= 1);
assert.throws(() => encode(Type.DATA, 0, 0));
assert.throws(() => encode(Type.DATA, 1, 0, new Uint8Array(MAX_PAYLOAD + 1)));
console.log("PASS transfer frame (JS)");
