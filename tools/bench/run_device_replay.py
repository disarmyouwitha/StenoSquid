#!/usr/bin/env python3
"""Manually start a paced USB replay using a worst-case fixture on the board."""

import argparse
from contextlib import ExitStack, suppress
from ctypes import CDLL, CFUNCTYPE, c_bool, c_char_p, c_void_p, cast
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from threading import Event, Thread
from time import monotonic
import tkinter as tk

from run_dictionary_benchmark import open_console, parse_outline, percentile


def bench(console, operation, *, timeout=10):
    reply = console.command(f"dictionary_benchmark {operation}", timeout=timeout)
    if reply.startswith("ERR"):
        raise RuntimeError(reply)
    return reply


def load_tape(data, wpm, prefix_strokes=None):
    phase = data["phases"]["300"]
    events = phase["events"]
    chords = [parse_outline(outline) for _, outline, _ in events]
    if not chords or any(len(chord) != 1 for chord in chords) or len(chords) != phase["chords"]:
        raise ValueError("The device replay needs one outline stroke per event.")
    words = phase["words"]
    if not words:
        raise ValueError("The fixture must produce text.")
    interval = round(60_000_000 * words / (wpm * len(chords)))
    if not 10_000 <= interval <= 5_000_000:
        raise ValueError("The fixture cadence is outside the firmware timer range.")
    expected = phase["expected_text"]
    if prefix_strokes is not None:
        if not 2 <= prefix_strokes <= len(chords):
            raise ValueError("Choose a prefix of at least two strokes within the tape.")
        chords = chords[:prefix_strokes]
        expected = events[prefix_strokes - 1][2]
        words = len(expected.split())
        if not words:
            raise ValueError("The prefix must produce text.")
    return [chord[0] for chord in chords], expected, words, interval


def inspect_board(console, fixture):
    status = json.loads(console.command("user_dictionary_status"))
    catalog = json.loads(console.command("native_catalog"))
    if (not status["ready"] or status["read_only"] or status["maintenance"]
            or sum(row["enabled"] for row in catalog) != fixture["active_dictionaries"]
            or status["tail"] != fixture["recent_edits"]):
        raise ValueError("The installed catalog or journal tail does not match the fixture.")
    for outline, expected, _ in fixture["probes"]:
        actual = console.command(f"lookup_stroke {outline}")
        if actual != ("ERR No translation" if expected is None else expected):
            raise ValueError(f"Lookup differs from fixture for {outline}: {actual!r}")
    return status, catalog


def latency(values):
    if not values:
        return {}
    return {**{f"p{p}": round(percentile(values, p / 100), 3) for p in (50, 95, 99)},
            "max": round(max(values), 3)}


def save_report(path, report, *, create=False):
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        return
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(report, stream, indent=2)
            stream.write("\n")
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def foreground_check():
    if sys.platform != "darwin":
        return lambda: True
    # Tk can retain widget focus while a different app is frontmost on macOS.
    CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
    objc = CDLL("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.argtypes = objc.sel_registerName.argtypes = [c_char_p]
    objc.objc_getClass.restype = objc.sel_registerName.restype = c_void_p
    send = cast(objc.objc_msgSend, c_void_p).value
    app = CFUNCTYPE(c_void_p, c_void_p, c_void_p)(send)(
        objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
    activate = CFUNCTYPE(None, c_void_p, c_void_p, c_bool)(send)
    activate(app, objc.sel_registerName(b"activateIgnoringOtherApps:"), True)
    active = CFUNCTYPE(c_bool, c_void_p, c_void_p)(send)
    selector = objc.sel_registerName(b"isActive")
    return lambda: active(app, selector)


def summarize(report, masks, expected, words, interval, status, replay, capture, received):
    samples = capture["samples"]
    timestamps = replay["times_ms"]
    timer = capture["timer_hz"]
    # Both time stamps come from the board; subtraction wraps at uint32.
    completion = [(sample[2] - emitted) & 0xffffffff for sample, emitted in zip(samples, timestamps)]
    observed_wpm = None
    if len(timestamps) > 1:
        duration = ((timestamps[-1] - timestamps[0]) & 0xffffffff) + interval / 1000
        observed_wpm = round(60_000 * words / duration, 2)
    strokes_match = ([sample[0] for sample in samples] == masks
                     and all(sample[3] == 1 for sample in samples))
    # Javelin can carry sentence capitalization and a leading word separator
    # over from the previous editor. Compare all text after that boundary exactly.
    capitalized = expected[:1].upper() + expected[1:]
    text_matches = received in (expected, capitalized, " " + expected, " " + capitalized)
    overruns = sum(value * 1000 >= interval for value in completion)
    scheduled_wpm = 60_000_000 * words / (interval * len(masks))
    cadence_matches = observed_wpm is not None and abs(observed_wpm - scheduled_wpm) <= scheduled_wpm / 100
    passed = (strokes_match and text_matches and cadence_matches and not overruns and not capture["overflow"]
              and not replay["aborted"] and replay["sent"] == len(masks) == len(timestamps)
              and status["ready"] and not status["read_only"]
              and status["generation"] == report["state_before"]["generation"]
              and status["input_overflows"] == report["state_before"]["input_overflows"])
    report.update(result="pass" if passed else "fail", state_after=status,
                  expected_text=expected, captured_text=received, exact_text=text_matches,
                  verbatim_text=expected == received,
                  exact_strokes=strokes_match, cadence_matches=cadence_matches,
                  deadline_overruns=overruns, observed_wpm=observed_wpm,
                  engine_ms=latency([sample[1] * 1000 / timer for sample in samples]),
                  emission_to_completion_ms=latency(completion), device_replay=replay, device_trace=capture)


def summarize_loop(report, masks, expected, words, interval, status, replay, capture, received):
    samples = capture["samples"]
    duration = ((replay["last_ms"] - replay["first_ms"]) & 0xffffffff) + interval / 1000
    observed = 60_000 * words / duration
    strokes_match = (len(samples) == capture["completed"] == replay["sent"] == len(masks) * report["repeats"]
                     and not capture["mismatches"] and not replay["aborted"] and not capture["overflow"])
    capitalized = expected[:1].upper() + expected[1:]
    text_matches = received in (expected, capitalized, " " + expected, " " + capitalized)
    scheduled_wpm = 60_000_000 * words / (interval * report["strokes"])
    cadence_matches = abs(observed - scheduled_wpm) <= scheduled_wpm / 100
    overruns = sum(sample[1] * 1000 >= interval for sample in samples)
    saturated = capture.get("engine_saturations", sum(sample[0] == 65535 for sample in samples))
    passed = (strokes_match and text_matches and cadence_matches and not overruns
              and status["ready"] and not status["read_only"]
              and status["generation"] == report["state_before"]["generation"]
              and status["input_overflows"] == report["state_before"]["input_overflows"])
    report.update(result="pass" if passed else "fail", state_after=status,
                  expected_text=expected, captured_text=received, exact_text=text_matches,
                  verbatim_text=expected == received, exact_strokes=strokes_match,
                  cadence_matches=cadence_matches, observed_wpm=round(observed, 2),
                  deadline_overruns=overruns, engine_sample_saturations=saturated,
                  engine_ms=latency([sample[0] / 1000 for sample in samples]),
                  emission_to_completion_ms=latency([sample[1] for sample in samples]),
                  device_replay=replay, device_trace=capture)


def capture_window(args, report, fixture, masks, expected, words, interval):
    window = tk.Tk()
    window.title("StenoSquid hardware typing replay")
    prompt = ("Click Start replay for the short diagnostic." if "prefix_strokes" in report
              else f"Click Start replay for the {args.wpm} WPM run.")
    message = tk.StringVar(value=prompt)
    tk.Label(window, textvariable=message, wraplength=740).pack(pady=6)
    editor = tk.Text(window, width=90, height=12, wrap="word")
    editor.pack(padx=10, pady=6)
    keypress_text = []
    keypress_events = []
    active = False
    collecting = False
    collected = Event()
    connection = ExitStack()
    console = None
    drain_deadline = 0.0

    def stop_device():
        for operation in ("replay stop", "capture stop"):
            with suppress(OSError, RuntimeError):
                bench(console, operation)

    def record_keypress(event):
        # Compare the incoming Tk key events with the Text widget's own editing.
        if not active:
            return
        if "prefix_strokes" in report:
            keypress_events.append([event.time, event.keysym, event.char])
        if event.keysym == "BackSpace" or event.char == "\b":
            if keypress_text:
                keypress_text.pop()
        elif event.char and event.char.isprintable():
            keypress_text.extend(event.char)

    def cancel(reason):
        nonlocal active
        if collecting:
            return
        if active or report["result"] == "armed":
            report.update(result="fail", error=reason, captured_text=editor.get("1.0", "end-1c"),
                          host_keypress_text="".join(keypress_text))
            save_report(args.report, report)
            if active:
                active = False
                stop_device()
            connection.close()
        window.quit()

    def finish():
        nonlocal active, collecting
        if not active:
            return
        active = False
        collecting = True
        received = editor.get("1.0", "end-1c")
        report.update(result="collecting", captured_text=received, host_keypress_text="".join(keypress_text))
        if "prefix_strokes" in report:
            report["host_keypress_events"] = keypress_events
        save_report(args.report, report)
        message.set("Typing finished. Reading diagnostics…")
        Thread(target=collect_diagnostics, args=(received,), name="replay-diagnostics").start()
        window.after(50, show_result)

    def collect_diagnostics(received):
        started = monotonic()
        stage = "replay status"
        try:
            replay = json.loads(bench(console, "replay status"))
            report["device_replay"] = replay
            save_report(args.report, report)
            stop_device()
            stage = "capture read"
            trace = json.loads(bench(console, "capture read", timeout=30))
            report["device_trace"] = trace
            save_report(args.report, report)
            stage = "dictionary status"
            status = json.loads(console.command("user_dictionary_status"))
            report["state_after"] = status
            stage = "result calculation"
            summarize_run = summarize if args.repeat == 1 else summarize_loop
            summarize_run(report, masks, expected, words, interval, status, replay, trace, received)
        except (RuntimeError, OSError, ValueError, KeyError, TypeError, IndexError) as error:
            report.update(result="incomplete", error=f"{stage}: {error}")
            if stage == "replay status":
                stop_device()
        finally:
            try:
                connection.close()
                report["diagnostics_seconds"] = round(monotonic() - started, 3)
                save_report(args.report, report)
            except (RuntimeError, OSError) as error:
                report.update(result="incomplete", error=f"finalizing the report: {error}")
            finally:
                collected.set()

    def show_result():
        nonlocal collecting
        if not collected.is_set():
            window.after(50, show_result)
            return
        collecting = False
        if report["result"] == "incomplete":
            message.set(f"Typing saved. Diagnostics incomplete. {report['error']}")
        else:
            message.set(f"Replay {report['result']}. Report saved.")
        button.configure(text="Close", state="normal", command=window.quit)

    def wait_for_text():
        if not active:
            return
        text = editor.get("1.0", "end-1c")
        capitalized = expected[:1].upper() + expected[1:]
        if text in (expected, capitalized, " " + expected, " " + capitalized) or monotonic() >= drain_deadline:
            finish()
        else:
            message.set("Waiting for the final keyboard reports…")
            window.after(150, wait_for_text)

    def start():
        nonlocal active, console, drain_deadline
        if active or report["result"] != "armed":
            return
        editor.delete("1.0", "end")
        editor.focus_set()
        if not in_foreground():
            message.set("Bring this window to the front and click Start replay.")
            return
        button.configure(state="disabled")
        message.set("Checking the current dictionaries and preparing the tape…")
        window.update_idletasks()
        try:
            console = connection.enter_context(open_console(args.serial))
            status, catalog = inspect_board(console, fixture)
            bench(console, "replay clear")
            bench(console, "replay hints on")
            for mask in masks:
                bench(console, f"replay add {mask:x}")
            if not in_foreground():
                raise RuntimeError("Bring this window to the front before starting the replay.")
        except (RuntimeError, OSError, ValueError, KeyError) as error:
            connection.close()
            message.set(f"{error} Click Start replay to retry.")
            report["start_error"] = str(error)
            save_report(args.report, report)
            button.configure(state="normal")
            return
        active = True
        report.update(state_before=status, catalog=catalog)
        report.pop("start_error", None)
        report["result"] = "running"
        save_report(args.report, report)
        strokes = len(masks) * args.repeat
        message.set(f"Capturing {strokes} strokes. Leave the keys alone. Escape stops the test.")
        try:
            bench(console, "capture start")
            schedule = str(interval) if args.repeat == 1 else f"{interval} {args.repeat}"
            bench(console, f"replay start {schedule}")
        except (RuntimeError, OSError, TimeoutError) as error:
            cancel(str(error))
            return
        print(f"Replay started with the capture textbox focused ({strokes} strokes).", flush=True)
        window.after(40, watch_focus)
        replay_ms = math.ceil(3000 + strokes * interval / 1000)
        drain_deadline = monotonic() + replay_ms / 1000 + 15
        window.after(replay_ms, wait_for_text)

    def in_foreground():
        return app_active() and editor.focus_displayof() is editor

    def watch_focus():
        if active:
            if not in_foreground():
                cancel("Capture editor lost foreground focus.")
            else:
                window.after(40, watch_focus)

    def lost_focus(_event):
        if active:
            cancel("Capture editor lost focus.")

    button = tk.Button(window, text="Start replay", command=start)
    button.pack(pady=6)
    editor.bind("<KeyPress>", record_keypress)
    editor.bind("<FocusOut>", lost_focus)
    window.bind("<Escape>", lambda _: cancel("Stopped by operator."))
    window.protocol("WM_DELETE_WINDOW", lambda: cancel("Capture window closed."))
    try:
        window.deiconify()
        window.update_idletasks()
        window.lift()
        app_active = foreground_check()
        editor.focus_force()
        print("Replay window is open. Click Start replay when ready.", flush=True)
        if args.start_when_focused:
            window.after(350, start)
        window.mainloop()
    finally:
        if active:
            stop_device()
        if not collecting:
            connection.close()
        window.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="Exact USB serial of the replay board")
    parser.add_argument("--fixture", type=Path, required=True, help="fixture.json from run_worst_case_replay.py")
    parser.add_argument("--wpm", type=int, choices=(300, 350, 400), required=True)
    parser.add_argument("--repeat", type=int, default=1, help="Repeat the tape at a continuous cadence")
    parser.add_argument("--prefix-strokes", type=int, help="Replay only the first N strokes at the full tape's cadence")
    parser.add_argument("--start-when-focused", action="store_true",
                        help="Try to start immediately after the capture window takes focus")
    parser.add_argument("--report", type=Path, required=True, help="New JSON report path")
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Choose a new report path.")
    fixture_bytes = args.fixture.read_bytes()
    fixture = json.loads(fixture_bytes)
    try:
        masks, expected, words, interval = load_tape(fixture, args.wpm, args.prefix_strokes)
    except ValueError as error:
        parser.error(str(error))
    if args.repeat < 1:
        parser.error("--repeat must be positive.")
    expected = " ".join([expected] * args.repeat)
    words *= args.repeat
    report = dict(at_utc=datetime.now(timezone.utc).isoformat(), target_wpm=args.wpm,
                  fixture_sha256=hashlib.sha256(fixture_bytes).hexdigest(), words=words,
                  strokes=len(masks) * args.repeat, repeats=args.repeat,
                  interval_us=interval, planned_chords_per_second=1_000_000 / interval,
                  scope="Onboard replay with USB capture. Physical key scanning, split input and Bluetooth are excluded.")
    if args.prefix_strokes is not None:
        report["prefix_strokes"] = args.prefix_strokes
    with open_console(args.serial) as console:
        meta = json.loads(bench(console, "meta"))
        if meta["firmware"] != "hardware-typing-replay" or len(masks) > meta["capture_capacity"]:
            parser.error("This tape requires the opt-in firmware replay build with sufficient capture capacity.")
        if args.repeat > 1 and len(masks) * args.repeat > meta.get("replay_loop_capacity", 0):
            parser.error("This loop requires the opt-in firmware with sufficient loop capture capacity.")
        print("Checking live fixture lookups…", flush=True)
        status, catalog = inspect_board(console, fixture)
        report.update(firmware=meta, serial=console.serial, state_before=status, catalog=catalog,
                      expected_text=expected, result="armed")
    save_report(args.report, report, create=True)
    capture_window(args, report, fixture, masks, expected, words, interval)
    print(f"{report['result']}: {args.report}")
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
