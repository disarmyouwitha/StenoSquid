#include "practice_model.h"
#include "practice_progress.h"
#include "split_status.h"

#include <errno.h>
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

static bool valid_record(const struct steno_squid_progress_record *record) {
    if (record->magic != STENO_SQUID_PROGRESS_MAGIC || record->pool != STENO_SQUID_WORD_SET_ID ||
        record->xp > STENO_SQUID_XP_MAX ||
        record->crc != crc32_ieee((const uint8_t *)record, offsetof(struct steno_squid_progress_record, crc))) {
        return false;
    }
    for (unsigned i = 0; i < STENO_SQUID_WORD_COUNT; i++) {
        if (record->words[i].first_try > record->words[i].completed ||
            record->words[i].last_session > record->session) return false;
    }
    return true;
}

static int load_progress(const char *name, size_t length, settings_read_cb read, void *arg) {
    if (strcmp(name, "progress")) return -ENOENT;
    if (length != sizeof(struct steno_squid_progress_record)) return -EINVAL;
    struct steno_squid_progress_record record;
    const int count = read(arg, &record, sizeof(record));
    if (count < 0) return count;
    if ((size_t)count != sizeof(record) || !valid_record(&record)) return -EINVAL;
    k_mutex_lock(&progress_mutex, K_FOREVER);
    if (!model.run.active) model.record = record;
    k_mutex_unlock(&progress_mutex);
    return 0;
}
SETTINGS_STATIC_HANDLER_DEFINE(steno_squid_practice, "steno_squid/practice", NULL,
                               load_progress, NULL, NULL);

static void save_progress(struct k_work *work);
K_WORK_DEFINE(save_progress_work, save_progress);
static void save_progress(struct k_work *work) {
    struct steno_squid_progress_record record;
    k_mutex_lock(&progress_mutex, K_FOREVER);
    const uint32_t version = generation;
    if (version == saved_generation) {
        k_mutex_unlock(&progress_mutex);
        return;
    }
    record = model.record;
    k_mutex_unlock(&progress_mutex);
    record.crc = crc32_ieee((const uint8_t *)&record, offsetof(struct steno_squid_progress_record, crc));
    const int err = settings_save_one("steno_squid/practice/progress", &record, sizeof(record));
    k_mutex_lock(&progress_mutex, K_FOREVER);
    if (!err) saved_generation = version;
    const bool changed = !err && generation != version;
    k_mutex_unlock(&progress_mutex);
    if (err) LOG_WRN("Practice progress save failed (%d)", err);
    if (changed) k_work_submit(&save_progress_work);
}

void steno_squid_words_begin(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    steno_squid_model_begin(&model, sys_rand32_get);
    generation++;
    k_mutex_unlock(&progress_mutex);
}
bool steno_squid_trouble_begin(void) {
    k_mutex_lock(&progress_mutex, K_FOREVER);
    const bool started = steno_squid_model_trouble_begin(&model, sys_rand32_get);
    if (started) generation++;
    k_mutex_unlock(&progress_mutex);
    return started;
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
    const uint64_t packed = (uint64_t)model.record.xp |
        ((uint64_t)run->first_try << 20) | ((uint64_t)run->errors << 24) |
        ((uint64_t)(average > 511 ? 511 : average) << 30) |
        ((uint64_t)run->earned << 39) | ((uint64_t)run->review << 46) |
        ((uint64_t)(steno_squid_trouble_count(&model.record) == 0) << 53);
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
