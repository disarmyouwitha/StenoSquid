#!/usr/bin/env python3
"""Test the real settings adapter with an in-memory, fault-injectable store."""
from hostlib import compiler as host_compiler
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
    static int (*registered_load)(const char *,size_t,settings_read_cb,void *)=set; \
    static int (*registered_commit)(void)=commit
int settings_save_one(const char *,const void *,size_t);
int settings_load_subtree_direct(const char *,int (*)(const char *,size_t,settings_read_cb,void *,void *),void *);
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
struct disk_value { char key[64]; unsigned size; uint8_t bytes[256]; };
static struct disk_value disk[64];
static unsigned disk_count, writes, largest;
static bool fail_header, race;
static struct disk_value *find(const char *key) {
    for (unsigned i=0;i<disk_count;i++) if(!strcmp(disk[i].key,key)) return &disk[i];
    return NULL;
}
uint32_t sys_rand32_get(void) { static uint32_t seed=7;seed=seed*1664525u+1013904223u;return seed; }
uint32_t crc32_ieee(const uint8_t *p,size_t n) {
    uint32_t crc=~0u;
    while(n--) { crc^=*p++;for(unsigned i=0;i<8;i++)crc=(crc>>1)^((crc&1)?0xedb88320u:0); }
    return ~crc;
}
int settings_save_one(const char *key,const void *p,size_t length) {
    assert(!held && length<=256);
    writes++; if(length>largest)largest=length;
    if(fail_header && !strcmp(key,"steno_squid/practice/progress"))return -EIO;
    struct disk_value *value=find(key);
    if(!value) { assert(disk_count<64);value=&disk[disk_count++];snprintf(value->key,sizeof(value->key),"%s",key); }
    memcpy(value->bytes,p,length);value->size=length;
    if(race) { race=false;model.record.xp++;generation++; }
    return 0;
}
static int read_disk(void *arg,void *out,size_t size) {
    struct disk_value *value=arg;
    if(size<value->size)return -ENOMEM;
    memcpy(out,value->bytes,value->size);return value->size;
}
int settings_load_subtree_direct(const char *key,int (*callback)(const char *,size_t,settings_read_cb,void *,void *),void *arg) {
    struct disk_value *value=find(key);
    return value?callback("",value->size,read_disk,value,arg):0;
}
static void reboot(void) {
    steno_squid_model_init(&model);
    memset(&committed,0,sizeof(committed));memset(committed_crc,0,sizeof(committed_crc));
    header_loaded=false;generation=saved_generation=0;
    struct disk_value *header=find("steno_squid/practice/progress");
    if(header)assert(registered_load("progress",header->size,read_disk,header)==0);
    assert(registered_commit()==0);
}
int main(void) {
    steno_squid_words_begin();steno_squid_practice_end();save_progress(&save_progress_work);
    assert(!writes); // Merely entering/leaving does not write.
    steno_squid_words_begin();
    steno_squid_words_answer(0,false,1000);
    steno_squid_words_answer(0,true,2000);
    for(unsigned i=1;i<10;i++)steno_squid_words_answer(i,true,1500);
    steno_squid_practice_finish();
    const struct steno_squid_progress_record expected=model.record;
    save_progress(&save_progress_work);
    assert(writes>1 && writes<=11 && largest<=256 && saved_generation==generation);
    reboot();
    assert(model.record.xp==expected.xp && model.record.session==expected.session);
    assert(!memcmp(model.record.words,expected.words,sizeof(expected.words)));
    unsigned before=writes;
    save_progress(&save_progress_work);assert(writes==before);
    // A one-word edit costs one small block and the small commit header.
    model.record.words[5].misses++;generation++;
    save_progress(&save_progress_work);assert(writes==before+2);
    const uint16_t misses=model.record.words[5].misses;
    reboot();assert(model.record.words[5].misses==misses);
    // Power loss/failure after the new block, before the header: retain the old snapshot.
    model.record.words[5].misses++;generation++;fail_header=true;
    save_progress(&save_progress_work);assert(saved_generation!=generation);
    fail_header=false;reboot();assert(model.record.words[5].misses==misses);
    // Concurrent answers prevent a mixed snapshot from being committed.
    model.record.words[5].misses++;generation++;race=true;
    const int queued_before=queued;
    save_progress(&save_progress_work);assert(saved_generation!=generation && queued>queued_before);
    const uint32_t xp=model.record.xp;
    save_progress(&save_progress_work);assert(saved_generation==generation);
    reboot();assert(model.record.xp==xp && model.record.words[5].misses==misses+1);
    // Hand drills change only the header, not word blocks.
    before=writes;steno_squid_hands_begin();
    for(unsigned i=0;i<STENO_SQUID_HAND_ROUNDS;i++)steno_squid_hands_answer(i,true,1000);
    steno_squid_practice_finish();save_progress(&save_progress_work);
    assert(writes==before+1);
    // Old snapshots are deliberately rejected; no migration reader remains.
    assert(registered_load("progress",3624,read_disk,&disk[0])==-EINVAL);
    // Damaged referenced blocks are rejected, not silently used.
    char key[48];block_key(key,sizeof(key),0,committed.blocks[0]);
    struct disk_value *value=find(key);assert(value);value->bytes[8]^=1;
    reboot();assert(model.record.xp==0);
    const struct zmk_activity_state_changed idle={ZMK_ACTIVITY_IDLE};
    const int previous_queue=queued;practice_activity(&idle);assert(queued>previous_queue);
    puts("Practice block storage: small dirty saves, atomic commit, reboot, concurrent updates and corruption checks passed");
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix='steno-squid-progress-') as directory:
        output = Path(directory)
        (output / 'stubs.h').write_text(STUBS, encoding="utf-8")
        for name in ('zephyr/kernel.h', 'zephyr/random/random.h', 'zephyr/settings/settings.h',
                     'zephyr/sys/crc.h', 'zephyr/logging/log.h', 'zephyr/toolchain.h',
                     'zmk/activity.h', 'zmk/events/activity_state_changed.h'):
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#include "stubs.h"\n', encoding="utf-8")
        (output / 'test.c').write_text(HARNESS, encoding="utf-8")
        binary = output / 'test'
        subprocess.run([*host_compiler('c'), '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-I' + str(output), '-I' + str(ROOT / 'src/steno_squid'),
                        str(output / 'test.c'), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
        model_binary = output / 'model-test'
        subprocess.run([*host_compiler('c'), '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-O2', '-I' + str(output), str(ROOT / 'tools/tests/test_practice_model.c'),
                        '-o', str(model_binary)], check=True)
        subprocess.run([str(model_binary)], check=True)
        view_binary = output / 'view-test'
        subprocess.run([*host_compiler('c'), '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', '-O2',
                        '-I' + str(output), str(ROOT / 'tools/tests/test_practice_view.c'),
                        '-o', str(view_binary)], check=True)
        subprocess.run([str(view_binary)], check=True)


if __name__ == '__main__':
    main()
