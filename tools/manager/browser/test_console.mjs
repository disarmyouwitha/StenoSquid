// node tools/manager/browser/test_console.mjs   (no browser required)
// Gate for Manager milestone M3 (console): console.js must write the same reports, return or raise the same thing, read as
// often and sleep for the same times as Python's Console on the same scripted device and clock. golden_console.py supplies
// the vectors.
import assert from "node:assert/strict";
import fs from "node:fs";
import {Console, REPORT_BYTES} from "./console.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/console_vectors.json", import.meta.url), "utf8"));
assert.equal(REPORT_BYTES, vectors.reportBytes);
const hex = (bytes) => Buffer.from(bytes).toString("hex");

for (const c of vectors.cases) {
  const clock = {value: 1000.0, sleeps: [], now() { return this.value; }, async sleep(seconds) { this.sleeps.push(seconds); this.value += seconds; }};
  const state = {writes: [], reads: 0, queue: [], prefix: []};
  const device = {
    async write(report) { assert.equal(report.length, REPORT_BYTES); state.writes.push(hex(report)); state.prefix = report.slice(0, 4); },
    async read(timeout) {
      state.reads++;
      const item = state.queue.shift();
      if (!item) { clock.value += timeout / 1000; return []; }
      clock.value += 0.002;
      const packet = [...(item.p ? state.prefix : []), ...Buffer.from(item.hex, "hex")];
      while (item.pad && packet.length < REPORT_BYTES) packet.push(0);
      return packet;
    },
  };
  const console_ = new Console(device, {clock});
  const results = [];
  for (const command of c.commands) {
    state.queue = command.script.slice();
    try {
      results.push({reply: command.timeout === null ? await console_.command(command.text) : await console_.command(command.text, command.timeout)});
    } catch (error) {
      results.push({error});
    }
  }
  assert.equal(results.length, c.expect.results.length, c.title);
  results.forEach((actual, i) => {
    const expected = c.expect.results[i];
    if ("error" in expected) {
      assert.ok(actual.error, `${c.title}: command ${i}: Python raised ${expected.error.type} (${expected.error.message}) but JS returned`);
      // Python's own messages are compared; library errors (a bad UTF-8 reply) only need to fail.
      if (["TimeoutError", "IndexError"].includes(expected.error.type)) {
        assert.equal(actual.error.name, expected.error.type, `${c.title}: command ${i}: kind of failure`);
        assert.equal(actual.error.message, expected.error.message, `${c.title}: command ${i}: message`);
      }
    } else {
      assert.ok(!actual.error, `${c.title}: command ${i}: JS raised ${actual.error?.message} but Python returned`);
      assert.equal(actual.reply, expected.reply, `${c.title}: command ${i}: reply`);
    }
  });
  assert.deepEqual(state.writes, c.expect.writes, `${c.title}: reports written`);
  assert.equal(state.reads, c.expect.reads, `${c.title}: number of reads`);
  assert.deepEqual(clock.sleeps, c.expect.sleeps, `${c.title}: sleeps`);
  assert.equal(Math.round((clock.value - 1000.0) * 1e6) / 1e6, c.expect.clock, `${c.title}: elapsed time`);
  assert.equal(console_.channel, c.expect.channel, `${c.title}: channel`);
}
const defaults = new Console({}, {clock: {}});
assert.equal(defaults.transport, "usb");
assert.equal(defaults.serial, null);
assert.equal(new Console({}, {serial: "AB", transport: "bluetooth", clock: {}}).transport, "bluetooth");
console.log(`PASS vendor console (JS): ${vectors.cases.length} scripted exchanges behave like Python's Console`);
