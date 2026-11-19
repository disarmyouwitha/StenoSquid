#pragma once
#include "native_maintenance.h"
#include <cstdint>
namespace candidate_b {
// A blocking flash operation cannot be preempted. Between operations, cap both
// CPU time and step count, and immediately yield to newly queued physical input.
template <class Step, class Clock, class Waiting>
JobResult NativeWorkSlice(Step step, Clock clock, Waiting waiting, uint32_t &steps) {
    steps = 0;
    const uint32_t start = clock();
    JobResult result = JobResult::More;
    while (steps < 128 && !waiting()) {
        result = step();
        ++steps;
        if (result != JobResult::More || uint32_t(clock() - start) >= 2000)
            break;
    }
    return result;
}
} // namespace candidate_b
