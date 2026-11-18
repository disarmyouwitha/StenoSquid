#include "native_input_queue.h"
#include <cassert>
#include <deque>
#include <iostream>
using namespace candidate_b;
int main() {
    NativeInputQueue<8> q;
    NativeInputEvent e;
    assert(q.Offer(1, true, 10) == InputOffer::Immediate);
    q.Pause();
    q.Pause();
    assert(q.Offer(1, false, 20) == InputOffer::Queued);
    q.Resume();
    assert(!q.BeginDrain() && !q.Pop(e));
    q.Resume();
    assert(q.BeginDrain() && q.Pop(e) && e.key == 1 && !e.pressed && e.when == 20);
    // An arrival after the last pop cannot overtake mutex release/FinishDrain.
    assert(q.Offer(2, true, 21) == InputOffer::Queued);
    assert(q.Pop(e) && e.key == 2 && e.when == 21);
    q.FinishDrain();
    assert(q.Offer(2, false, 22) == InputOffer::Immediate);
    // A failed immediate delivery gets its original timestamp exactly once.
    q.Retry(3, true, 0xfffffff0);
    q.Offer(3, false, 4);
    assert(q.BeginDrain() && q.Pop(e) && e.when == 0xfffffff0 && e.pressed);
    assert(q.Pop(e) && e.when == 4 && !e.pressed);
    q.FinishDrain();
    // Mode changes discard pending strokes and every release of a held chord.
    q.Pause();
    q.Offer(4, true, 30);
    q.Cancel(31);
    q.Resume();
    assert(q.BeginDrain() && q.Pop(e) && e.key == 255 && e.when == 31);
    assert(!q.Pop(e));
    q.FinishDrain();
    assert(q.Offer(5, true, 32) == InputOffer::Discarded);
    assert(q.Offer(4, false, 33) == InputOffer::Discarded);
    assert(q.Offer(5, false, 34) == InputOffer::Discarded);
    assert(q.BeginDrain() && q.Pop(e) && e.key == 255);
    q.FinishDrain();
    assert(q.Offer(6, true, 35) == InputOffer::Immediate);
    assert(q.Offer(6, false, 36) == InputOffer::Immediate);
    // Overflow abandons the entire batch, not just one key from a chord.
    q.Pause();
    for (unsigned i = 0; i < 8; ++i)
        assert(q.Offer(i, true, 100 + i) == InputOffer::Queued);
    assert(q.Offer(8, true, 108) == InputOffer::Discarded && q.Count() == 0);
    assert(q.Overflows() == 1 && q.Highwater() == 8);
    for (unsigned i = 0; i < 9; ++i)
        assert(q.Offer(i, false, 120 + i) == InputOffer::Discarded);
    q.Resume();
    assert(q.BeginDrain() && q.Pop(e) && e.key == 255 && !q.Pop(e));
    q.FinishDrain();
    assert(!q.Pending());
    // Deterministic stress: FIFO order, timestamps, bounded partial drains and
    // arrivals while draining. No overwrite or duplicate across 50,000 batches.
    NativeInputQueue<32> stress;
    std::deque<NativeInputEvent> expected;
    uint32_t random = 1, when = 0xfffff000;
    for (unsigned round = 0; round < 50000; ++round) {
        stress.Pause();
        random = random * 1664525 + 1013904223;
        unsigned pairs = 1 + (random % 12);
        for (unsigned pair = 0; pair < pairs; ++pair) {
            uint8_t key = uint8_t((round + pair) % 32);
            for (bool down : {true, false}) {
                NativeInputEvent item{when++, key, down};
                expected.push_back(item);
                assert(stress.Offer(key, down, item.when) == InputOffer::Queued);
            }
        }
        stress.Resume();
        assert(stress.BeginDrain());
        while (stress.Pop(e)) {
            assert(!expected.empty());
            auto want = expected.front();
            expected.pop_front();
            assert(e.key == want.key && e.pressed == want.pressed && e.when == want.when);
        }
        stress.FinishDrain();
        assert(expected.empty() && !stress.Pending());
    }
    assert(stress.Overflows() == 0);
    std::cout << "Native input queue: FIFO, nested pauses, wraparound time, retry, mode "
                 "cancellation, overflow recovery and 50,000 batches passed\n";
}
