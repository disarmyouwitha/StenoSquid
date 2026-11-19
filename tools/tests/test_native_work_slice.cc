#include "native_work_slice.h"
#include <cassert>
#include <iostream>
using namespace candidate_b;
int main() {
    uint32_t time = 0, steps = 0, calls = 0;
    auto clock = [&] { return time; };
    auto idle = [] { return false; };
    assert(NativeWorkSlice(
               [&] {
                   ++calls;
                   time += 300;
                   return JobResult::More;
               },
               clock, idle, steps) == JobResult::More);
    assert(steps == 7 && calls == 7);
    calls = 0;
    assert(NativeWorkSlice(
               [&] {
                   ++calls;
                   return JobResult::More;
               },
               clock, idle, steps) == JobResult::More);
    assert(steps == 128 && calls == 128); // Frozen clocks still cannot monopolize work.
    calls = 0;
    time = 0xfffffff0;
    assert(NativeWorkSlice(
               [&] {
                   ++calls;
                   time += 1100;
                   return JobResult::More;
               },
               clock, idle, steps) == JobResult::More);
    assert(steps == 2 && calls == 2); // Timer wrap.
    calls = 0;
    assert(NativeWorkSlice(
               [&] {
                   ++calls;
                   return JobResult::More;
               },
               clock, [&] { return calls == 3; }, steps) == JobResult::More);
    assert(steps == 3);
    calls = 0;
    assert(NativeWorkSlice(
               [&] {
                   ++calls;
                   return JobResult::More;
               },
               clock, [] { return true; }, steps) == JobResult::More);
    assert(steps == 0 && calls == 0);
    for (auto terminal :
         {JobResult::Done, JobResult::Corrupt, JobResult::Busy, JobResult::NoSpace}) {
        assert(NativeWorkSlice([&] { return terminal; }, clock, idle, steps) == terminal &&
               steps == 1);
    }
    calls = 0;
    NativeWorkSlice(
        [&] {
            ++calls;
            time += 250000;
            return JobResult::More;
        },
        clock, idle, steps);
    assert(steps == 1 && calls == 1); // A slow erase cannot trigger a second step.
    std::cout << "Native work slices: time/step bounds, physical input priority, timer wrap, "
                 "terminal results and slow erase yielding passed\n";
}
