// node tools/manager/browser/test_editor.mjs   (no browser required)
// Gate for the dictionary editor: on each scripted keyboard session, editor.js must send the same console commands (same
// timeouts, same order), report the same progress, look up bundled factory JSON the same way, and give each step the same
// result or failure as manager_editor.py (document cache included). golden_editor.py wrote the vectors.
import assert from "node:assert/strict";
import fs from "node:fs";
import {makeEditor, kibibytes, dumpsIndented} from "./editor.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/editor_vectors.json", import.meta.url), "utf8"));

const resolve = (value, last) => {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const keys = Object.keys(value);
    if (keys.length === 1 && keys[0] === "$last") return last[value.$last];
    return Object.fromEntries(keys.map((key) => [key, resolve(value[key], last)]));
  }
  return value;
};

function sameFailure(label, expected, error) {
  assert.ok(error, `${label}: Python raised ${expected.type} (${expected.message}) but JS returned a result`);
  if (expected.type === "RuntimeError") assert.equal(error.kind, "RuntimeError", `${label}: should be a RuntimeError, got ${error.name}: ${error.message}`);
  else if (expected.type === "ValueError") assert.ok(error.kind !== "RuntimeError" && error.name === "Error", `${label}: should be a ValueError, got ${error.name}: ${error.message}`);
  else assert.equal(error.name, expected.type, `${label}: kind of failure (${error.message})`);
  if (["ValueError", "RuntimeError"].includes(expected.type)) {
    // Python's JSON decoder wording and its own int()/unpack texts are not a contract; everything the editor wrote is.
    let wanted = expected.message, got = error.message;
    const json = /^(Invalid JSON at line \d+, column \d+): /.exec(wanted);
    if (json) { wanted = json[1]; got = got.slice(0, wanted.length); }
    assert.equal(got, wanted, `${label}: error text`);
  }
}

let steps = 0;
for (const c of vectors.cases) {
  let next = 0;
  const take = (kind) => {
    const event = c.events[next++];
    assert.ok(event, `${c.title}: JS did more than Python (extra ${kind})`);
    assert.equal(event.e, kind, `${c.title}: step ${next - 1}: Python did '${event.e}' but JS did '${kind}'`);
    return event;
  };
  const console_ = {
    serial: c.serial,
    async command(text, timeout = 30) {
      const event = take("cmd");
      assert.equal(text, event.c, `${c.title}: command ${next - 1}`);
      assert.equal(timeout, event.t, `${c.title}: timeout of ${text}`);
      if ("d" in event) {
        const count = Number(text.split(" ").at(-1));
        const data = new Uint8Array(count).fill(0xff);
        data.set(Buffer.from(event.d, "hex"));
        return "OK " + Buffer.from(data).toString("base64");
      }
      return event.r;
    },
  };
  const progress = (message) => assert.equal(message, take("progress").m, `${c.title}: progress message`);
  const factorySource = async (identity, name, digest) => {
    const event = take("factory");
    assert.deepEqual([identity, name, digest], event.a, `${c.title}: factory lookup`);
    return event.ret === null ? null : new Map(event.ret);
  };
  const clock = {now: () => 1000, async sleep() { throw new Error("no waiting expected"); }};
  const editor = makeEditor({factorySource, clock});
  const last = {};
  for (const step of c.steps) {
    const [kind] = step;
    let value = null, error = null;
    try {
      if (kind === "setstatus") {
        // the scripted keyboard changes its status; the replies are in the recorded commands
      } else if (kind === "read") {
        const selected = {...vectors.selections[step[1]], ...(step[3] ?? {})};
        value = await editor.readDocument(console_, selected, progress, step[2]);
        last.version = value.version; last.source = value.source;
      } else if (kind === "change") {
        const selected = {...vectors.selections[step[1]], ...(step[4] ?? {})};
        value = await editor.changeDocument(console_, selected, resolve(step[2], last), progress, {apply: step[3]});
        if (!step[3]) last.token = value.token; else { last.version = value.version; last.source = value.source; }
      } else if (kind === "clear") {
        editor.clearDocuments(step[1]);
      } else throw new Error(kind);
    } catch (e) {
      if (e?.code === "ERR_ASSERTION") throw e;
      error = e;
    }
    const expected = take("step").r;
    if ("error" in expected) sameFailure(`${c.title}: step ${steps}`, expected.error, error);
    else {
      assert.ok(!error, `${c.title}: JS raised ${error?.name}: ${error?.message}`);
      assert.deepEqual(JSON.parse(JSON.stringify(value)), expected.ok, `${c.title}: result`);
    }
    steps++;
  }
  assert.equal(next, c.events.length, `${c.title}: JS stopped after ${next} of ${c.events.length} events; Python's next was '${c.events[next]?.e}'`);
}
for (const [received, text] of vectors.kib) assert.equal(kibibytes(received), text, `progress size of ${received} bytes`);
for (const [pairs, text] of vectors.dump) assert.equal(dumpsIndented(new Map(pairs)) + "\n", text + "\n", `document text of ${JSON.stringify(pairs).slice(0, 60)}`);
console.log(`PASS dictionary editor (JS): ${vectors.cases.length} sessions, ${steps} steps behave like manager_editor.py`);
