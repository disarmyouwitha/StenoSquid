#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "practice_words.h"

#define STENO_SQUID_PROGRESS_MAGIC 0x31505453u /* STP1 */
#define STENO_SQUID_XP_MAX 0xfffffu
#define STENO_SQUID_NO_REVIEW 127u

struct steno_squid_word_stats {
    uint16_t completed;
    uint16_t first_try;
    uint16_t misses;
    uint16_t mean_ds;
    uint32_t last_session;
};

struct steno_squid_progress_record {
    uint32_t magic;
    uint32_t pool;
    uint32_t session;
    uint32_t xp;
    uint32_t rounds;
    struct steno_squid_word_stats words[STENO_SQUID_WORD_COUNT];
    uint32_t crc;
};

struct steno_squid_word_run {
    uint8_t deck[STENO_SQUID_WORD_ROUNDS];
    uint8_t position;
    uint8_t first_try;
    uint8_t errors;
    uint8_t word_errors;
    uint8_t earned;
    uint8_t review;
    uint8_t review_errors;
    uint8_t timed_count;
    uint32_t response_ds;
    uint8_t hand_streak;
    bool hand;
    bool active;
    bool finished;
};

struct steno_squid_practice_model {
    struct steno_squid_progress_record record;
    struct steno_squid_word_run run;
};

static inline uint16_t steno_squid_sat16(uint16_t value) {
    return value == UINT16_MAX ? value : value + 1;
}

static inline void steno_squid_model_init(struct steno_squid_practice_model *model) {
    memset(model, 0, sizeof(*model));
    model->record.magic = STENO_SQUID_PROGRESS_MAGIC;
    model->record.pool = STENO_SQUID_WORD_SET_ID;
    model->run.review = STENO_SQUID_NO_REVIEW;
}

/* Failed/slow words and words not practised recently get more chances. All
 * words keep a nonzero weight, including previously mastered words. */
static inline unsigned steno_squid_word_weight(const struct steno_squid_progress_record *record,
                                              unsigned word) {
    const struct steno_squid_word_stats *stats = &record->words[word];
    const uint32_t age = record->session >= stats->last_session
                             ? record->session - stats->last_session : 0;
    unsigned weight = 8;
    if (!stats->completed) weight += 24;
    weight += (uint32_t)stats->misses * 40 / ((uint32_t)stats->misses + stats->completed + 1);
    weight += (uint32_t)(stats->completed - stats->first_try) * 20 / (stats->completed + 1u);
    weight += stats->mean_ds / 20 > 16 ? 16 : stats->mean_ds / 20;
    weight += age > 20 ? 20 : age;
    return weight;
}

static inline void steno_squid_model_begin(struct steno_squid_practice_model *model,
                                          uint32_t (*random_word)(void)) {
    memset(&model->run, 0, sizeof(model->run));
    model->run.review = STENO_SQUID_NO_REVIEW;
    model->run.active = true;
    if (model->record.session < UINT32_MAX) model->record.session++;
    bool selected[STENO_SQUID_WORD_COUNT] = {false};
    for (unsigned round = 0; round < STENO_SQUID_WORD_ROUNDS; round++) {
        unsigned total = 0;
        for (unsigned i = 0; i < STENO_SQUID_WORD_COUNT; i++) {
            if (!selected[i]) total += steno_squid_word_weight(&model->record, i);
        }
        unsigned choice = ((uint64_t)random_word() * total) >> 32;
        for (unsigned i = 0; i < STENO_SQUID_WORD_COUNT; i++) {
            if (selected[i]) continue;
            const unsigned weight = steno_squid_word_weight(&model->record, i);
            if (choice < weight) {
                selected[i] = true;
                model->run.deck[round] = i;
                break;
            }
            choice -= weight;
        }
    }
}

/* Each round can be credited only once, even if feedback receives more keys.
 * Repeated errors earn no XP. Long pauses are excluded from response averages. */
static inline bool steno_squid_model_answer(struct steno_squid_practice_model *model,
                                           unsigned round, bool correct, uint32_t elapsed_ms) {
    struct steno_squid_word_run *run = &model->run;
    if (!run->active || run->hand || run->finished || round != run->position || round >= STENO_SQUID_WORD_ROUNDS) {
        return false;
    }
    const unsigned word = run->deck[round];
    struct steno_squid_word_stats *stats = &model->record.words[word];
    stats->last_session = model->record.session;
    if (!correct) {
        stats->misses = steno_squid_sat16(stats->misses);
        if (run->errors < 63) run->errors++;
        if (run->word_errors < UINT8_MAX) run->word_errors++;
        if (run->word_errors > run->review_errors) {
            run->review_errors = run->word_errors;
            run->review = word;
        }
        return true;
    }
    const unsigned reward = run->word_errors ? 2 : 10;
    if (!run->word_errors) run->first_try++;
    const bool previously_completed = stats->completed != 0;
    stats->completed = steno_squid_sat16(stats->completed);
    if (!run->word_errors) stats->first_try = steno_squid_sat16(stats->first_try);
    if (elapsed_ms <= 120000) {
        unsigned ds = elapsed_ms / 100;
        if (!ds) ds = 1;
        stats->mean_ds = previously_completed && stats->mean_ds
                             ? ((uint32_t)stats->mean_ds * 3 + ds) / 4 : ds;
        run->response_ds += ds;
        run->timed_count++;
    }
    run->earned += reward;
    model->record.xp = model->record.xp > STENO_SQUID_XP_MAX - reward
                           ? STENO_SQUID_XP_MAX : model->record.xp + reward;
    run->word_errors = 0;
    run->position++;
    return true;
}

static inline void steno_squid_model_hands_begin(struct steno_squid_practice_model *model) {
    memset(&model->run, 0, sizeof(model->run));
    model->run.review = STENO_SQUID_NO_REVIEW;
    model->run.active = model->run.hand = true;
}

/* One XP for a correct key. Every fifth consecutive correct key adds a bonus
 * of 1, 2, 3, 4, then 5 XP (capped there). A clean 50-key run totals 110 XP
 * including completion, fitting the same bounded result packet as WORDS. */
static inline bool steno_squid_model_hands_answer(struct steno_squid_practice_model *model,
                                                 unsigned round, bool correct, uint32_t elapsed_ms) {
    struct steno_squid_word_run *run = &model->run;
    if (!run->active || !run->hand || run->finished || round != run->position || round >= STENO_SQUID_HAND_ROUNDS) {
        return false;
    }
    if (!correct) {
        if (run->errors < 63) run->errors++;
        run->hand_streak = 0;
        return false; // Run feedback changed, but no persistent data changed.
    }
    run->hand_streak++;
    unsigned reward = 1;
    if (run->hand_streak % 5 == 0) {
        const unsigned bonus = run->hand_streak / 5;
        reward += bonus > 5 ? 5 : bonus;
    }
    run->earned += reward;
    model->record.xp = model->record.xp > STENO_SQUID_XP_MAX - reward
                           ? STENO_SQUID_XP_MAX : model->record.xp + reward;
    if (elapsed_ms <= 120000) {
        unsigned ds = elapsed_ms / 100;
        if (!ds) ds = 1;
        run->response_ds += ds;
        run->timed_count++;
    }
    run->position++;
    return true;
}

static inline bool steno_squid_model_finish(struct steno_squid_practice_model *model) {
    struct steno_squid_word_run *run = &model->run;
    if (!run->active || run->finished || run->position != (run->hand ? STENO_SQUID_HAND_ROUNDS : STENO_SQUID_WORD_ROUNDS)) return false;
    run->finished = true;
    run->earned += 20;
    model->record.xp = model->record.xp > STENO_SQUID_XP_MAX - 20
                           ? STENO_SQUID_XP_MAX : model->record.xp + 20;
    if (model->record.rounds < UINT32_MAX) model->record.rounds++;
    return true;
}

/* Level 1 starts at zero; each next level costs level * 100 XP. */
static inline unsigned steno_squid_pop_level(uint32_t xp, unsigned *into, unsigned *needed) {
    unsigned level = 1;
    while (xp >= level * 100u) {
        xp -= level * 100u;
        level++;
    }
    if (into) *into = xp;
    if (needed) *needed = level * 100u;
    return level;
}
