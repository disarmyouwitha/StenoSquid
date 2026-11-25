"""Binary upload client for the experimental transfer session (Phase 2 prototype).

Used by native_update.publish() in the desktop and browser (Pyodide) Managers.
Frames and records mirror src/steno_squid/transfer_frame.h and
transfer_staged_sink.h; see docs/transfer-session.md. The legacy Base64 console
path stays the default and the fallback: if the keyboard does not advertise the
transfer, or refuses BEGIN before any data moved, publish() carries on exactly as
before. Once data has been accepted a failure is raised, never retried over
Base64 behind the user's back.
"""
import os
import re
import struct
import time
import zlib

MAGIC = 0xB5
HEADER = 10
TRAILER = 4
MAX_PAYLOAD = 240
SEGMENT = 65536
REPORT_BYTES = 63

BEGIN, DATA, END, COMMIT, ABORT = 1, 2, 3, 4, 5
ACK, NAK, ERROR, DONE = 0x81, 0x82, 0x83, 0x84
NAK_OUT_OF_ORDER, NAK_BAD_OFFSET, NAK_SINK_BUSY = 1, 2, 3
STATE_OPEN, STATE_RECEIVING, STATE_VERIFIED = 1, 2, 3
RECORD_ERASE, RECORD_WRITE = 1, 2
WRITE_BYTES = 256
MAX_WINDOW = 16

ERROR_TEXT = {
    1: "another transfer is active", 2: "the transfer session was lost", 3: "unexpected transfer state",
    4: "invalid transfer length", 5: "the data failed its checksum", 6: "the keyboard refused the data",
    7: "the keyboard could not verify the staged data", 8: "the keyboard could not save the catalog",
    9: "the transfer timed out", 10: "the transfer was cancelled", 11: "invalid transfer parameters",
}


class TransferUnavailable(RuntimeError):
    """Nothing was sent that matters: the caller may use the Base64 console path."""


def encode(frame_type, session, offset, payload=b""):
    if not session or len(payload) > MAX_PAYLOAD:
        raise ValueError("Invalid transfer frame")
    body = struct.pack("<BBHIH", MAGIC, frame_type, session, offset, len(payload)) + payload
    return body + struct.pack("<I", zlib.crc32(body))


class Decoder:
    """Resynchronising stream decoder; mirrors the firmware's drop-one-byte rule."""

    def __init__(self):
        self.buffer = bytearray()
        self.crc_errors = 0

    def push(self, data):
        self.buffer.extend(data)
        frames = []
        while True:
            while self.buffer and self.buffer[0] != MAGIC:
                del self.buffer[0]
            if len(self.buffer) < HEADER:
                return frames
            _, frame_type, session, offset, length = struct.unpack_from("<BBHIH", self.buffer)
            if frame_type == 0 or session == 0 or length > MAX_PAYLOAD:
                del self.buffer[0]
                continue
            total = HEADER + length + TRAILER
            if len(self.buffer) < total:
                return frames
            if zlib.crc32(bytes(self.buffer[:total - TRAILER])) != struct.unpack_from("<I", self.buffer, total - TRAILER)[0]:
                self.crc_errors += 1
                del self.buffer[0]
                continue
            frames.append((frame_type, session, offset, bytes(self.buffer[HEADER:HEADER + length])))
            del self.buffer[:total]


def records(update):
    """The staged update as ERASE/WRITE records, in the order publish() sends them."""
    out = bytearray()
    for segment, block in update.segments.items():
        out += struct.pack("<BH", RECORD_ERASE, segment)
        for offset in range(0, len(block), WRITE_BYTES):
            chunk = block[offset:offset + WRITE_BYTES]
            if chunk == b"\xff" * len(chunk):
                continue
            if len(chunk) % 4:
                raise ValueError("Staged data must be a multiple of four bytes")
            out += struct.pack("<BIH", RECORD_WRITE, segment * SEGMENT + offset, len(chunk)) + chunk
    return bytes(out)


def probe(console):
    """Chunk size and staged-update kind, or TransferUnavailable. Sends nothing destructive."""
    if not getattr(console, "binary_transfer", True) or not hasattr(console, "device"):
        raise TransferUnavailable("binary transfer disabled")
    try:
        reply = console.command("native_install update transfer-capabilities", timeout=3)
        match = re.fullmatch(r"OK XFER1 (\d+)", reply)
        if not match:
            raise TransferUnavailable(reply)
        kind = re.fullmatch(r"OK (\d+)", console.command("native_install update transfer-kind", timeout=3))
        if not kind:
            raise TransferUnavailable("no staged update")
    except (TimeoutError, RuntimeError, OSError) as error:
        if isinstance(error, TransferUnavailable):
            raise
        raise TransferUnavailable(str(error)) from error
    chunk = int(match[1])
    if not 1 <= chunk <= min(MAX_PAYLOAD, REPORT_BYTES - HEADER - TRAILER):
        raise TransferUnavailable("unsupported chunk size")
    return chunk, int(kind[1])


class Session:
    def __init__(self, device, session):
        self.device = device
        self.session = session
        self.decoder = Decoder()

    def send(self, frame_type, offset, payload=b""):
        frame = encode(frame_type, self.session, offset, payload)
        written = self.device.write(b"\0" + frame.ljust(REPORT_BYTES, b"\0"))
        if written <= 0:
            raise RuntimeError("HID write failed. Check the left half’s connection.")

    def receive(self, timeout):
        packet = self.device.read(REPORT_BYTES, int(timeout * 1000))
        frames = self.decoder.push(bytes(packet)) if packet else []
        return [frame for frame in frames if frame[1] == self.session]


def _error(code):
    return RuntimeError("Binary transfer failed: " + ERROR_TEXT.get(code, f"error {code}") + ".")


def upload(console, update, progress, *, window=8, stall_seconds=1.0, retries=6, sleep=time.sleep):
    """Stream, verify and commit `update` over the binary session."""
    chunk, kind = probe(console)
    stream = records(update)
    total = len(stream)
    session = Session(console.device, struct.unpack("<H", os.urandom(2))[0] or 1)
    window = max(1, min(window, MAX_WINDOW))
    begin = struct.pack("<IIHHB", total, zlib.crc32(stream), chunk, window, kind) + struct.pack(
        "<III", update.metadata, len(update.payload), zlib.crc32(update.payload))

    for _ in range(3):  # BEGIN is idempotent until data arrives.
        session.send(BEGIN, 0, begin)
        reply = next(iter(session.receive(1.0)), None)
        if reply is None:
            continue
        if reply[0] == ACK and reply[3][0] == STATE_OPEN:
            window = max(1, min(window, struct.unpack_from("<H", reply[3], 1)[0]))
            break
        raise TransferUnavailable(ERROR_TEXT.get(reply[3][0], "refused") if reply[0] == ERROR else "refused")
    else:
        raise TransferUnavailable("no reply to BEGIN")

    try:
        _stream(session, stream, total, chunk, window, stall_seconds, retries, sleep, progress)
        _finish(session, total, sleep)
    except BaseException:
        try:
            session.send(ABORT, 0)
        except (OSError, RuntimeError):
            pass
        raise


def _stream(session, stream, total, chunk, window, stall_seconds, retries, sleep, progress):
    acked = sent = 0
    last_percent = -5
    stalls = 0
    last_progress = time.monotonic()
    last_rewind = (-1, 0.0)
    progress("Writing dictionary data… 0%")
    while acked < total:
        while sent < total and sent - acked < window * chunk:
            session.send(DATA, sent, stream[sent:sent + chunk])
            sent = min(total, sent + chunk)
        frames = session.receive(0.05)
        for frame_type, _, offset, payload in frames:
            if frame_type == ERROR:
                raise _error(payload[0])
            if frame_type not in (ACK, NAK) or offset > total:
                continue
            if offset > acked:
                acked = offset
                stalls = 0
                last_progress = time.monotonic()
            if frame_type == NAK:
                # One rewind per gap: later NAKs for frames already in flight are noise.
                now = time.monotonic()
                if acked != last_rewind[0] or now - last_rewind[1] > 0.2:
                    sent = acked
                    last_rewind = (acked, now)
                    if payload[0] == NAK_SINK_BUSY:
                        sleep(0.002)
        percent = acked * 100 // total
        if percent >= last_percent + 5 or (percent == 100 and last_percent != 100):
            progress(f"Writing dictionary data… {percent}%")
            last_percent = percent
        if not frames and time.monotonic() - last_progress > stall_seconds:
            stalls += 1
            if stalls > retries:
                raise RuntimeError("The keyboard stopped responding during the transfer. Check the connection.")
            sent = acked  # Go back N: resend from the last acknowledged byte.
            last_progress = time.monotonic()


def _wait(session, wanted, resend, deadline, sleep, every):
    """Send `resend`, then wait for a frame in `wanted`.

    END and COMMIT are idempotent, so a silent link is probed by repeating them every
    `every` seconds (slowly for COMMIT: the keyboard is busy and cannot answer). A NAK
    sink-busy asks for an immediate polite repeat.
    """
    resend()
    last = time.monotonic()
    while time.monotonic() < deadline:
        for frame_type, _, offset, payload in session.receive(0.05):
            if frame_type == ERROR:
                raise _error(payload[0])
            if frame_type in wanted:
                return frame_type, payload
            if frame_type == NAK:
                sleep(0.002)
                resend()
                last = time.monotonic()
        if time.monotonic() - last >= every:
            resend()
            last = time.monotonic()
    raise TimeoutError("No reply from the keyboard during the transfer.")

def _finish(session, total, sleep):
    progress_deadline = time.monotonic() + 120
    while True:  # Verify runs in bounded slices on the keyboard; each busy NAK asks us to repeat END.
        frame_type, payload = _wait(session, {ACK}, lambda: session.send(END, total), progress_deadline, sleep, 0.5)
        if payload[0] == STATE_VERIFIED:
            break
    _wait(session, {DONE}, lambda: session.send(COMMIT, total), time.monotonic() + 1800, sleep, 2.0)
