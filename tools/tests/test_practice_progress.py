#!/usr/bin/env python3
"""Test the real settings adapter with an in-memory, fault-injectable store."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
STUBS = r'''
#pragma once
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define CONFIG_ZMK_LOG_LEVEL 3
#define LOG_MODULE_DECLARE(...)
#define LOG_WRN(...)
#define K_MUTEX_DEFINE(name) int name
#define K_FOREVER 0
static int held;
static inline void k_mutex_lock(int *m,int timeout) { assert(!held);held=1; }
static inline void k_mutex_unlock(int *m) { assert(held);held=0; }
struct k_work { void (*handler)(struct k_work *); };
#define K_WORK_DEFINE(name, fn) struct k_work name={fn}
static int queued;
static inline int k_work_submit(struct k_work *work) { queued++;return 0; }
uint32_t sys_rand32_get(void);
typedef int (*settings_read_cb)(void *,void *,size_t);
#define SETTINGS_STATIC_HANDLER_DEFINE(name,path,get,set,commit,export) \
    static int (*registered_load)(const char *,size_t,settings_read_cb,void *)=set
int settings_save_one(const char *,const void *,size_t);
uint32_t crc32_ieee(const uint8_t *,size_t);
enum zmk_activity_state { ZMK_ACTIVITY_ACTIVE,ZMK_ACTIVITY_IDLE,ZMK_ACTIVITY_SLEEP };
struct zmk_activity_state_changed { enum zmk_activity_state state; };
typedef struct zmk_activity_state_changed zmk_event_t;
static inline const struct zmk_activity_state_changed *as_zmk_activity_state_changed(const zmk_event_t *event) { return event; }
#define ZMK_LISTENER(...)
#define ZMK_SUBSCRIPTION(...)
#define ZMK_EV_EVENT_BUBBLE 0
#define BUILD_ASSERT(x,...) _Static_assert(x,#x)
'''
HARNESS = r'''
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include "practice_progress.c"
static uint8_t disk[2048];
static size_t disk_size;
static unsigned writes;
static bool failed, race;
uint32_t sys_rand32_get(void) { static uint32_t seed=7;seed=seed*1664525u+1013904223u;return seed; }
uint32_t crc32_ieee(const uint8_t *p,size_t n) {
    uint32_t crc=~0u;
    while(n--) { crc^=*p++;for(unsigned i=0;i<8;i++)crc=(crc>>1)^((crc&1)?0xedb88320u:0); }
    return ~crc;
}
int settings_save_one(const char *key,const void *p,size_t length) {
    assert(!held && !strcmp(key,"steno_squid/practice/progress"));
    assert(length<=sizeof(disk));writes++;
    if(failed)return -EIO;
    memcpy(disk,p,length);disk_size=length;
    if(race) { race=false;model.record.xp++;generation++; }
    return 0;
}
static int read_disk(void *arg,void *out,size_t size) { memcpy(out,disk,disk_size);return disk_size; }
static int read_short(void *arg,void *out,size_t size) { memcpy(out,disk,disk_size-1);return disk_size-1; }
static int read_error(void *arg,void *out,size_t size) { return -EIO; }
int main(void) {
    assert(steno_squid_practice_no_trouble(steno_squid_practice_progress()));
    assert(!steno_squid_trouble_begin() && generation==0);
    steno_squid_words_begin();
    for(unsigned i=0;i<10;i++) {
        assert(steno_squid_words_prompt(i)<100);
        steno_squid_words_answer(i,true,1500);
        assert(writes==0 && queued==0); // Never queue a write on every key/chord.
    }
    steno_squid_practice_finish();
    assert(queued && model.record.xp==120);
    const uint64_t summary=steno_squid_practice_progress();
    assert(steno_squid_practice_first_try(summary)==10);
    assert(steno_squid_practice_average(summary)==15);
    assert(steno_squid_practice_xp_earned(summary)==120);
    assert(steno_squid_practice_review(summary)==127);
    save_progress(&save_progress_work);
    assert(writes==1 && saved_generation==generation);
    save_progress(&save_progress_work);assert(writes==1);
    const struct steno_squid_progress_record expected=model.record;
    steno_squid_model_init(&model);generation=saved_generation=0;
    assert(registered_load("progress",disk_size,read_disk,NULL)==0);
    assert(model.record.xp==120 && model.record.rounds==1);
    assert(!memcmp(model.record.words,expected.words,sizeof(expected.words)));
    assert(registered_load("other",disk_size,read_disk,NULL)==-ENOENT);
    assert(registered_load("progress",disk_size-1,read_disk,NULL)==-EINVAL);
    assert(registered_load("progress",disk_size,read_short,NULL)==-EINVAL);
    assert(registered_load("progress",disk_size,read_error,NULL)==-EIO);
    disk[20]^=1;
    assert(registered_load("progress",disk_size,read_disk,NULL)==-EINVAL);
    assert(model.record.xp==120);disk[20]^=1;
    struct steno_squid_progress_record wrong;
    memcpy(&wrong,disk,sizeof(wrong));wrong.pool^=1;
    wrong.crc=crc32_ieee((const uint8_t *)&wrong,offsetof(struct steno_squid_progress_record,crc));
    memcpy(disk,&wrong,sizeof(wrong));
    assert(registered_load("progress",disk_size,read_disk,NULL)==-EINVAL);
    steno_squid_words_begin();steno_squid_words_answer(0,false,1000);
    failed=true;save_progress(&save_progress_work);
    assert(saved_generation!=generation);
    failed=false;race=true;const int requests=queued;
    save_progress(&save_progress_work);
    assert(saved_generation!=generation && queued>requests);
    save_progress(&save_progress_work);assert(saved_generation==generation);
    steno_squid_practice_end();assert(!model.run.active);
    zmk_event_t idle={ZMK_ACTIVITY_IDLE};
    const int previous=queued;practice_activity(&idle);assert(queued>previous);
    // Restored word stats and XP survive adding hand rewards to the same record.
    steno_squid_hands_begin();
    for(unsigned i=0;i<50;i++) steno_squid_hands_answer(i,true,1000);
    steno_squid_practice_finish();
    const uint32_t hand_xp=model.record.xp;
    save_progress(&save_progress_work);
    steno_squid_model_init(&model);generation=saved_generation=0;
    assert(registered_load("progress",disk_size,read_disk,NULL)==0);
    assert(model.record.xp==hand_xp);
    assert(model.record.rounds==expected.rounds+1);
    assert(!steno_squid_practice_no_trouble(steno_squid_practice_progress()));
    assert(steno_squid_trouble_begin());
    for(unsigned i=0;i<10;i++) steno_squid_words_answer(i,true,1000);
    steno_squid_practice_finish();
    const struct steno_squid_progress_record trouble=model.record;
    save_progress(&save_progress_work);
    steno_squid_model_init(&model);generation=saved_generation=0;
    assert(registered_load("progress",disk_size,read_disk,NULL)==0);
    assert(model.record.xp==trouble.xp && model.record.rounds==trouble.rounds);
    assert(!memcmp(model.record.words,trouble.words,sizeof(trouble.words)));
    assert(steno_squid_practice_no_trouble(steno_squid_practice_progress()));
    puts("Practice persistence: reboot restore, CRC/pool rejection, failures, concurrent updates and idle checkpoint passed");
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix='steno-squid-progress-') as directory:
        output = Path(directory)
        (output / 'stubs.h').write_text(STUBS)
        for name in ('zephyr/kernel.h', 'zephyr/random/random.h', 'zephyr/settings/settings.h',
                     'zephyr/sys/crc.h', 'zephyr/logging/log.h', 'zephyr/toolchain.h',
                     'zmk/activity.h', 'zmk/events/activity_state_changed.h'):
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#include "stubs.h"\n')
        (output / 'test.c').write_text(HARNESS)
        binary = output / 'test'
        subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-I' + str(output), '-I' + str(ROOT / 'src/steno_squid'),
                        str(output / 'test.c'), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
        model_binary = output / 'model-test'
        subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-O2', '-I' + str(output), str(ROOT / 'tools/tests/test_practice_model.c'),
                        '-o', str(model_binary)], check=True)
        subprocess.run([str(model_binary)], check=True)


if __name__ == '__main__':
    main()
