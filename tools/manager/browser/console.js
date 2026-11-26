// Manager milestone M3: the vendor console, ported from the Console class in tools/dictionaries/import_personal.py.
// Text commands travel in 63-byte reports, each starting with a channel prefix ("c07 "); the reply comes back the same
// way and ends with a blank line. `device` needs `write(bytes: number[]) -> Promise` (63 bytes, no report id) and
// `read(timeoutMs) -> Promise<number[]>` (empty on timeout); `clock` gives monotonic seconds and an awaitable sleep.
export const REPORT_BYTES = 63;

const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", {fatal: true});
const defaultClock = {now: () => performance.now() / 1000, sleep: (seconds) => new Promise((resolve) => setTimeout(resolve, seconds * 1000))};

export class ConsoleTimeout extends Error { constructor(message) { super(message); this.name = "TimeoutError"; } }

// Python's str.split() whitespace: the C0 spacing and separator controls, NEL, and the Unicode spaces (not the byte-order mark).
const PY_SPACE = new RegExp("[\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]+");
function firstWord(text) {
  const word = text.split(PY_SPACE).find((part) => part !== "");
  if (word === undefined) throw Object.assign(new Error("list index out of range"), {name: "IndexError"});
  return word;
}

const startsWith = (bytes, prefix) => prefix.length <= bytes.length && prefix.every((value, i) => bytes[i] === value);

export class Console {
  constructor(device, {serial = null, transport = "usb", clock = defaultClock} = {}) {
    this.device = device;
    this.serial = serial;
    this.transport = transport;
    this.clock = clock;
    this.channel = 0;
  }

  async command(text, timeout = 30) {
    const clock = this.clock;
    const deadline = clock.now() + timeout;
    for (;;) {
      const reply = await this.exchange(text, deadline);
      if (reply !== "ERR Native flash busy" || !text.startsWith("native_install ")) return reply;
      if (clock.now() >= deadline) return reply;
      await clock.sleep(Math.min(0.1, Math.max(0, deadline - clock.now())));
      if (clock.now() >= deadline) return reply;
    }
  }

  async exchange(text, deadline) {
    this.channel = (this.channel + 1) % 100;
    const prefix = Array.from(encoder.encode(`c${String(this.channel).padStart(2, "0")} `));
    const line = encoder.encode(text + "\n");
    const payload = REPORT_BYTES - prefix.length;
    for (let offset = 0; offset < line.length; offset += payload) {
      const report = new Array(REPORT_BYTES).fill(0);
      const chunk = [...prefix, ...line.subarray(offset, offset + payload)];
      for (let i = 0; i < chunk.length; i++) report[i] = chunk[i];
      await this.device.write(report);
    }
    let response = [];
    while (this.clock.now() < deadline) {
      const packet = await this.device.read(250);
      if (packet && packet.length) {
        let end = packet.length;
        while (end > 0 && packet[end - 1] === 0) end--;
        for (let i = 0; i < end; i++) response.push(packet[i]);
        if (response.length >= 2 && response[response.length - 1] === 10 && response[response.length - 2] === 10) {
          if (startsWith(response, prefix)) {
            let reply = decoder.decode(Uint8Array.from(response.slice(prefix.length)));
            while (reply.endsWith("\n")) reply = reply.slice(0, -1);
            return reply;
          }
          response = [];
        }
      }
    }
    throw new ConsoleTimeout(`No reply to ${firstWord(text)}. Check the left half’s connection.`);
  }
}
