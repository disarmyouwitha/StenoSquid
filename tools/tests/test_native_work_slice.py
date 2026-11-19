#!/usr/bin/env python3
"""Run the bounded native maintenance worker without a scheduler."""

from pathlib import Path
import subprocess
import tempfile
from hostlib import compiler, extract_c_function

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory() as directory:
        binary = Path(directory) / "native-work-slice"
        subprocess.run(
            [
                *compiler(),
                "-std=c++17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(ROOT / "tools/storage"),
                str(ROOT / "tools/tests/test_native_work_slice.cc"),
                "-o",
                str(binary),
            ],
            check=True,
        )
        subprocess.run([str(binary)], check=True)


def check_runtime_idle_predicate():
    runtime = (ROOT / "src/steno_squid/native_store_runtime.cc").read_text()
    begin = runtime.index("const auto result=NativeWorkSlice(")
    predicate_begin = runtime.index("[] {", begin)
    predicate_end = runtime.index("},steps)", predicate_begin) + 1
    predicate = runtime[predicate_begin:predicate_end]
    environment = extract_c_function(runtime, "bool IdleMaintenanceEnvironment()")
    allowed = extract_c_function(runtime, "bool IdleMaintenanceAllowed()")
    source = r"""
#include "native_input_queue.h"
#include "native_work_slice.h"
#include <cassert>
#include <iostream>
using namespace candidate_b;
NativeInputQueue<8> input;
bool usb_power = true, automatic_maintenance = true;
enum { ZMK_ACTIVITY_IDLE, ZMK_ACTIVITY_ACTIVE } activity = ZMK_ACTIVITY_IDLE;
bool zmk_usb_is_powered() { return usb_power; }
auto zmk_activity_get_state() { return activity; }
bool steno_squid_native_input_pending() { return input.Pending(); }
bool steno_squid_native_input_waiting() { return input.HasEvents(); }
struct { struct { bool published = false; bool Published() const { return published; } } checkpoint; } store;
""" + environment + allowed + "\nconst auto should_yield = " + predicate + r""";
int main() {
    assert(IdleMaintenanceAllowed());
    input.Pause();
    assert(!IdleMaintenanceAllowed() && !should_yield());
    uint32_t steps;
    const auto step = [] { return JobResult::Done; };
    const auto clock = [] { return 0u; };
    assert(NativeWorkSlice(step, clock, should_yield, steps) == JobResult::Done && steps == 1);
    activity = ZMK_ACTIVITY_ACTIVE;
    assert(should_yield());
    store.checkpoint.published = true;
    assert(!should_yield());
    input.Offer(2, true, 100);
    assert(should_yield());
    assert(NativeWorkSlice(step, clock, should_yield, steps) == JobResult::More && steps == 0);
    std::cout << "Automatic idle slice runs under its own pause and defers to new physical input\n";
}
"""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        unit = path / "automatic-idle-check.cc"
        unit.write_text(source)
        binary = path / "automatic-idle-check"
        subprocess.run([*compiler(), "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"), str(unit), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
    check_runtime_idle_predicate()
