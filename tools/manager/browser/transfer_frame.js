// Browser-side codec for the experimental binary transfer frames.
// Mirrors src/steno_squid/transfer_frame.h; see docs/transfer-session.md.
// Not wired into the Manager: the legacy Base64 console stays the default.
export const MAGIC = 0xb5;
export const HEADER = 10;
export const TRAILER = 4;
export const MAX_PAYLOAD = 240;
export const Type = {BEGIN: 1, DATA: 2, END: 3, COMMIT: 4, ABORT: 5, ACK: 0x81, NAK: 0x82, ERROR: 0x83, DONE: 0x84};

const TABLE = new Uint32Array(256).map((_, n) => {
  for (let k = 0; k < 8; k++) n = n & 1 ? (n >>> 1) ^ 0xedb88320 : n >>> 1;
  return n >>> 0;
});
export function crc32(bytes, crc = 0) {
  crc = ~crc >>> 0;
  for (const b of bytes) crc = (crc >>> 8) ^ TABLE[(crc ^ b) & 0xff];
  return ~crc >>> 0;
}

export function encode(type, session, offset, payload = new Uint8Array(0)) {
  if (!session || payload.length > MAX_PAYLOAD) throw new RangeError("Invalid transfer frame");
  const out = new Uint8Array(HEADER + payload.length + TRAILER);
  const view = new DataView(out.buffer);
  out[0] = MAGIC; out[1] = type;
  view.setUint16(2, session, true);
  view.setUint32(4, offset >>> 0, true);
  view.setUint16(8, payload.length, true);
  out.set(payload, HEADER);
  view.setUint32(HEADER + payload.length, crc32(out.subarray(0, HEADER + payload.length)), true);
  return out;
}

// Resynchronising stream decoder; mirrors the firmware's drop-one-byte rule.
export class Decoder {
  constructor() { this.buffer = new Uint8Array(0); this.crcErrors = 0; this.skipped = 0; }
  push(bytes) {
    const joined = new Uint8Array(this.buffer.length + bytes.length);
    joined.set(this.buffer); joined.set(bytes, this.buffer.length);
    this.buffer = joined;
  }
  *frames() {
    for (;;) {
      let start = 0;
      while (start < this.buffer.length && this.buffer[start] !== MAGIC) { start++; this.skipped++; }
      this.buffer = this.buffer.subarray(start);
      if (this.buffer.length < HEADER) return;
      const view = new DataView(this.buffer.buffer, this.buffer.byteOffset, this.buffer.byteLength);
      const length = view.getUint16(8, true);
      if (this.buffer[1] === 0 || view.getUint16(2, true) === 0 || length > MAX_PAYLOAD) {
        this.buffer = this.buffer.subarray(1); this.skipped++; continue;
      }
      const total = HEADER + length + TRAILER;
      if (this.buffer.length < total) return;
      if (crc32(this.buffer.subarray(0, total - TRAILER)) !== view.getUint32(total - TRAILER, true)) {
        this.buffer = this.buffer.subarray(1); this.skipped++; this.crcErrors++; continue;
      }
      const frame = {type: this.buffer[1], session: view.getUint16(2, true), offset: view.getUint32(4, true),
        payload: this.buffer.slice(HEADER, HEADER + length)};
      this.buffer = this.buffer.subarray(total);
      yield frame;
    }
  }
}
