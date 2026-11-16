#!/usr/bin/env python3
"""Run the bounded BLE transmitter with delayed completion and driver errors."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
STUBS = r'''
#pragma once
#include <cassert>
#include <cstdint>
#include <cstring>
#include <deque>
#include <vector>
#include <cerrno>
struct Done {};
struct k_spinlock {};
using k_spinlock_key_t = int;
static bool locked;
inline int k_spin_lock(k_spinlock *) { assert(!locked); locked = true; return 0; }
inline void k_spin_unlock(k_spinlock *, int) { assert(locked); locked = false; }
struct k_sem { int count; };
struct k_msgq { size_t size; std::deque<std::vector<uint8_t>> messages; };
#define K_MSGQ_DEFINE(n,s,c,a) k_msgq n{s,{}}
#define K_SEM_DEFINE(n,a,b) k_sem n{a}
#define K_THREAD_DEFINE(...)
#define K_FOREVER -1
#define K_NO_WAIT 0
#define K_SECONDS(n) ((n)*1000)
static void (*on_wait)();
static bool timeout_at_completion;
inline void k_sem_give(k_sem *s) { s->count = 1; }
inline void k_sem_reset(k_sem *s) { s->count = 0; }
inline int k_sem_take(k_sem *s, int timeout) {
    assert(!locked && timeout == 2000);
    if(on_wait) on_wait();
    if(!s->count || timeout_at_completion) return -EAGAIN;
    s->count = 0;
    return 0;
}
inline int k_msgq_put(k_msgq *q,const void *p,int timeout) {
    assert(timeout == K_NO_WAIT && q->messages.empty());
    q->messages.emplace_back((const uint8_t *)p,(const uint8_t *)p+q->size);
    return 0;
}
inline int k_msgq_get(k_msgq *q,void *p,int timeout) {
    assert(timeout == K_FOREVER);
    if(q->messages.empty()) throw Done{};
    memcpy(p,q->messages.front().data(),q->size);
    q->messages.pop_front();
    return 0;
}
'''
HARNESS = r'''
#include "stubs.h"
#include "javelin_ble_tx.c"
static int result;
static std::vector<uint8_t> sent;
int steno_squid_javelin_ble_notify(const void *data,size_t size,uint32_t session) {
    assert(!locked && session == 42 && size == JAVELIN_HID_REPORT_BYTES);
    sent.assign((const uint8_t *)data,(const uint8_t *)data+size);
    return result;
}
static void run() { try { tx_worker(nullptr,nullptr,nullptr); } catch (Done &) {} }
int main() {
    uint8_t first[JAVELIN_HID_REPORT_BYTES], second[JAVELIN_HID_REPORT_BYTES];
    memset(first, 1, sizeof(first)); memset(second, 2, sizeof(second));
    on_wait = run;
    assert(!steno_squid_javelin_ble_send(first,sizeof(first),42));
    assert(sent == std::vector<uint8_t>(sizeof(first),1));
    result = -ENOTCONN;
    assert(steno_squid_javelin_ble_send(first,sizeof(first),42) == -ENOTCONN);
    result = 0;
    on_wait = nullptr;
    assert(steno_squid_javelin_ble_send(first,sizeof(first),42) == -ETIMEDOUT);
    assert(tx_busy);
    assert(steno_squid_javelin_ble_send(second,sizeof(second),42) == -EBUSY);
    run(); // late stack completion must still own the original report
    assert(!tx_busy && sent == std::vector<uint8_t>(sizeof(first),1));
    on_wait = run;
    assert(!steno_squid_javelin_ble_send(second,sizeof(second),42));
    assert(sent == std::vector<uint8_t>(sizeof(second),2));
    timeout_at_completion = true;
    assert(!steno_squid_javelin_ble_send(first,sizeof(first),42));
    assert(!tx_busy);
    assert(steno_squid_javelin_ble_send(first,0,42) == -EINVAL);
}
'''


def main():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)
        (path / 'stubs.h').write_text(STUBS)
        for name in ('zephyr/kernel.h', 'zephyr/spinlock.h'):
            p = path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('#include "stubs.h"\n')
        (path / 'test.cc').write_text(HARNESS)
        subprocess.run(['c++', '-std=c++20', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-I', folder, '-I', str(ROOT / 'src/steno_squid'),
                        str(path / 'test.cc'), '-o', str(path / 'test')], check=True)
        subprocess.run([str(path / 'test')], check=True)
    print('PASS: BLE TX timeout, late completion, ownership, disconnect and recovery')


if __name__ == '__main__':
    main()
