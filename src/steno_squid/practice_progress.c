#include "practice_model.h"
#include "practice_progress.h"
#include "split_status.h"

#include <errno.h>
#include <stdio.h>
#include <zephyr/kernel.h>
#include <zephyr/random/random.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/crc.h>
#include <zephyr/logging/log.h>
#include <zmk/activity.h>
#include <zmk/events/activity_state_changed.h>

LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);
K_MUTEX_DEFINE(progress_mutex);
static struct steno_squid_practice_model model = {
    .record = {.magic = STENO_SQUID_PROGRESS_MAGIC, .pool = STENO_SQUID_WORD_SET_ID},
    .run = {.review = STENO_SQUID_NO_REVIEW},
};
static uint32_t generation, saved_generation;
/* STP2 is a clean format break: the owner opted to reset Practice progress.
 * A small commit record points at alternating copies of 16-word blocks. Only
 * changed blocks are written. The old commit stays usable until the new one
 * is saved, including if power fails after a block write. */
#define PROGRESS_HEADER_MAGIC 0x32505453u
#define WORDS_PER_BLOCK 16u
#define WORD_BLOCKS ((STENO_SQUID_WORD_COUNT + WORDS_PER_BLOCK - 1) / WORDS_PER_BLOCK)
struct progress_header {
    uint32_t magic, pool, session, xp;
    uint32_t blocks[WORD_BLOCKS];
    uint32_t crc;
};
struct progress_block {
    uint32_t generation;
    struct steno_squid_word_stats words[WORDS_PER_BLOCK];
    uint32_t crc;
};
static struct progress_header committed;
static uint32_t committed_crc[WORD_BLOCKS];
/* One small scratch block, serialized by the settings startup or save worker. */
static struct progress_block scratch;
static bool header_loaded;
BUILD_ASSERT(sizeof(struct progress_header) < 128);
BUILD_ASSERT(sizeof(struct progress_block) <= 256);

static unsigned words_in_block(unsigned index) {
    const unsigned remaining = STENO_SQUID_WORD_COUNT - index * WORDS_PER_BLOCK;
    return remaining < WORDS_PER_BLOCK ? remaining : WORDS_PER_BLOCK;
}
static void block_key(char *key, size_t size, unsigned index, uint32_t version) {
    snprintf(key, size, "steno_squid/practice/words/%u/%u", index, version & 1u);
}
static bool valid_header(const struct progress_header *header) {
    return header->magic == PROGRESS_HEADER_MAGIC && header->pool == STENO_SQUID_WORD_SET_ID &&
        header->xp <= STENO_SQUID_XP_MAX && header->crc == crc32_ieee((const uint8_t *)header,
            offsetof(struct progress_header, crc));
}
static int load_progress(const char *name, size_t length, settings_read_cb read, void *arg) {
    if (!strncmp(name, "words/", 6)) return 0; // Loaded on demand after the commit header.
    if (strcmp(name, "progress")) return -ENOENT;
    if (length != sizeof(committed)) return -EINVAL;
    struct progress_header next;
    const int count = read(arg, &next, sizeof(next));
    if (count < 0) return count;
    if ((size_t)count != sizeof(next) || !valid_header(&next)) return -EINVAL;
    committed = next;
    header_loaded = true;
    return 0;
}
struct block_read { uint32_t expected; unsigned index; bool found; };
static int read_block(const char *name, size_t length, settings_read_cb read, void *arg, void *parameter) {
    struct block_read *request = parameter;
    if (name && name[0]) return 0;
    if (length != sizeof(scratch)) return -EINVAL;
    const int count = read(arg, &scratch, sizeof(scratch));
    if (count != sizeof(scratch) || scratch.generation != request->expected ||
        scratch.crc != crc32_ieee((const uint8_t *)&scratch, offsetof(struct progress_block, crc))) return -EINVAL;
    for (unsigned i = 0; i < words_in_block(request->index); ++i) {
        if (scratch.words[i].first_try > scratch.words[i].completed ||
            scratch.words[i].last_session > committed.session) return -EINVAL;
    }
    memcpy(model.record.words + request->index * WORDS_PER_BLOCK, scratch.words,
           words_in_block(request->index) * sizeof(scratch.words[0]));
    committed_crc[request->index] = crc32_ieee((const uint8_t *)scratch.words, sizeof(scratch.words));
    request->found = true;
    return 0;
}
static int commit_progress(void) {
    if (!header_loaded) return 0;
    k_mutex_lock(&progress_mutex, K_FOREVER);
    if (model.run.active) { k_mutex_unlock(&progress_mutex); return -EBUSY; }
    bool valid = true;
    for (unsigned i = 0; i < WORD_BLOCKS; ++i) {
        if (!committed.blocks[i]) continue;
        char key[48];
        block_key(key, sizeof(key), i, committed.blocks[i]);
        struct block_read request = {.expected = committed.blocks[i], .index = i};
        if (settings_load_subtree_direct(key, read_block, &request) != 0 || !request.found) { valid = false; break; }
    }
    if (valid) {
        model.record.session = committed.session;
        model.record.xp = committed.xp;
    } else {
        steno_squid_model_init(&model);
        memset(&committed, 0, sizeof(committed));
        memset(committed_crc, 0, sizeof(committed_crc));
        LOG_WRN("Practice progress block failed verification; starting fresh");
    }
    k_mutex_unlock(&progress_mutex);
    return 0;
}
SETTINGS_STATIC_HANDLER_DEFINE(steno_squid_practice, "steno_squid/practice", NULL,
                               load_progress, commit_progress, NULL);

static void save_progress(struct k_work *work);
K_WORK_DEFINE(save_progress_work, save_progress);
static void save_progress(struct k_work *work) {
    struct progress_header next;
    uint32_t next_crc[WORD_BLOCKS];
    k_mutex_lock(&progress_mutex, K_FOREVER);
    const uint32_t version = generation;
    if (version == saved_generation) { k_mutex_unlock(&progress_mutex); return; }
    next = committed;
    next.magic = PROGRESS_HEADER_MAGIC;
    next.pool = STENO_SQUID_WORD_SET_ID;
    next.session = model.record.session;
    next.xp = model.record.xp;
    k_mutex_unlock(&progress_mutex);
    int err = 0;
    for (unsigned i = 0; i < WORD_BLOCKS; ++i) {
        memset(&scratch, 0, sizeof(scratch));
        k_mutex_lock(&progress_mutex, K_FOREVER);
        memcpy(scratch.words, model.record.words + i * WORDS_PER_BLOCK,
               words_in_block(i) * sizeof(scratch.words[0]));
        k_mutex_unlock(&progress_mutex);
        next_crc[i] = crc32_ieee((const uint8_t *)scratch.words, sizeof(scratch.words));
        // A never-saved block of zeros needs no flash record.
        bool empty = true;
        const uint8_t *bytes = (const uint8_t *)scratch.words;
        for (unsigned j = 0; j < sizeof(scratch.words); ++j) if (bytes[j]) { empty = false; break; }
        if ((!committed.blocks[i] && empty) || (committed.blocks[i] && next_crc[i] == committed_crc[i])) continue;
        scratch.generation = committed.blocks[i] + 1;
        if (!scratch.generation) scratch.generation = 2; // Keep alternating banks at wrap.
        scratch.crc = crc32_ieee((const uint8_t *)&scratch, offsetof(struct progress_block, crc));
        char key[48];
        block_key(key, sizeof(key), i, scratch.generation);
        err = settings_save_one(key, &scratch, sizeof(scratch));
        if (err) break;
        next.blocks[i] = scratch.generation;
    }
    k_mutex_lock(&progress_mutex, K_FOREVER);
    const bool changed = generation != version;
    k_mutex_unlock(&progress_mutex);
    if (!err && !changed) {
        next.crc = crc32_ieee((const uint8_t *)&next, offsetof(struct progress_header, crc));
        err = settings_save_one("steno_squid/practice/progress", &next, sizeof(next));
        if (!err) {
            committed = next;
            memcpy(committed_crc, next_crc, sizeof(next_crc));
            k_mutex_lock(&progress_mutex, K_FOREVER);
            saved_generation = version;
            k_mutex_unlock(&progress_mutex);
        }
    }
    if (err) LOG_WRN("Practice progress save failed (%d)", err);
    if (changed) k_work_submit(&save_progress_work);
}

void steno_squid_words_begin(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    steno_squid_model_begin(&model, sys_rand32_get);
    k_mutex_unlock(&progress_mutex);
}
bool steno_squid_trouble_begin(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    const bool started = steno_squid_model_trouble_begin(&model, sys_rand32_get);
    k_mutex_unlock(&progress_mutex);
    return started;
}
unsigned steno_squid_practice_review_word(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    const unsigned word = model.run.review;
    k_mutex_unlock(&progress_mutex);
    return word;
}
unsigned steno_squid_words_prompt(unsigned round) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    const unsigned word = round < STENO_SQUID_WORD_ROUNDS ? model.run.deck[round] : model.run.deck[STENO_SQUID_WORD_ROUNDS - 1];
    k_mutex_unlock(&progress_mutex);
    return word;
}
void steno_squid_words_answer(unsigned round, bool correct, uint32_t elapsed_ms) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    if (steno_squid_model_answer(&model, round, correct, elapsed_ms)) generation++;
    k_mutex_unlock(&progress_mutex);
}
void steno_squid_hands_begin(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    steno_squid_model_hands_begin(&model);
    k_mutex_unlock(&progress_mutex);
}
void steno_squid_multi_begin(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    steno_squid_model_multi_begin(&model, sys_rand32_get);
    k_mutex_unlock(&progress_mutex);
}
void steno_squid_build_begin(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    steno_squid_model_build_begin(&model, sys_rand32_get);
    k_mutex_unlock(&progress_mutex);
}
void steno_squid_speed_timeout(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    if (steno_squid_model_timeout(&model)) generation++;
    k_mutex_unlock(&progress_mutex);
    steno_squid_practice_checkpoint();
}
void steno_squid_hands_answer(unsigned round, bool correct, uint32_t elapsed_ms) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    if (steno_squid_model_hands_answer(&model, round, correct, elapsed_ms)) generation++;
    k_mutex_unlock(&progress_mutex);
}
void steno_squid_practice_finish(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    if (steno_squid_model_finish(&model)) generation++;
    k_mutex_unlock(&progress_mutex);
    steno_squid_practice_checkpoint();
}
void steno_squid_practice_checkpoint(void) { k_work_submit(&save_progress_work); }
void steno_squid_practice_end(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    model.run.active = false;
    k_mutex_unlock(&progress_mutex);
    steno_squid_practice_checkpoint();
}

uint64_t steno_squid_practice_progress(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    const struct steno_squid_word_run *run = &model.run;
    const unsigned average = run->timed_count ? run->response_ds / run->timed_count : 0;
    const uint64_t packed = steno_squid_practice_pack_progress(model.record.xp,
        run->first_try, run->errors, average, run->earned,
        steno_squid_trouble_count(&model.record) == 0);
    k_mutex_unlock(&progress_mutex);
    return packed;
}

/* Checkpoint on idle, well before the normal deep-sleep timeout. No clock or
 * computer is needed. Save requests also happen at completion and on exit. */
static int practice_activity(const zmk_event_t *event) {
    const struct zmk_activity_state_changed *activity = as_zmk_activity_state_changed(event);
    if (activity && activity->state != ZMK_ACTIVITY_ACTIVE) steno_squid_practice_checkpoint();
    return ZMK_EV_EVENT_BUBBLE;
}
ZMK_LISTENER(steno_squid_practice_progress, practice_activity);
ZMK_SUBSCRIPTION(steno_squid_practice_progress, zmk_activity_state_changed);
