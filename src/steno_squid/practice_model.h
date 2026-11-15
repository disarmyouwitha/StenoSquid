#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "practice_words.h"
#include "practice_multi.h"

#define STENO_SQUID_PROGRESS_MAGIC 0x31505453u /* STP1 */
#define STENO_SQUID_XP_MAX 0xfffffu
#define STENO_SQUID_NO_REVIEW UINT16_MAX

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
    uint16_t deck[STENO_SQUID_WORD_ROUNDS];
    uint8_t position;
    uint8_t limit;
    uint8_t first_try;
    uint8_t errors;
    uint8_t word_errors;
    uint8_t earned;
    uint16_t review;
    uint8_t review_errors;
    uint8_t timed_count;
    uint32_t response_ds;
    uint8_t hand_streak;
    bool hand; /* Hands/columns/Multi path: no per-word pool records. */
    bool multi;
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

/* Trouble is drawn only from learned words. A few successful repetitions can
 * resolve an early mistake; later practice uses lifetime first-try accuracy
 * and the response-time moving average rather than unseen words. */
static inline bool steno_squid_word_trouble(const struct steno_squid_word_stats *stats) {
    return (stats->misses && stats->completed < 3) ||
           (stats->completed && ((uint32_t)stats->first_try * 10 < (uint32_t)stats->completed * 9 ||
                                 stats->mean_ds > 40));
}
static inline unsigned steno_squid_trouble_count(const struct steno_squid_progress_record *record) {
    unsigned count = 0;
    for (unsigned i = 0; i < STENO_SQUID_WORD_COUNT; i++) count += steno_squid_word_trouble(&record->words[i]);
    return count;
}

static inline void steno_squid_model_deck(struct steno_squid_practice_model *model,
                                         const bool *eligible, unsigned available,
                                         uint32_t (*random_word)(void)) {
    bool selected[STENO_SQUID_WORD_COUNT] = {false};
    unsigned remaining = available;
    for (unsigned round = 0; round < STENO_SQUID_WORD_ROUNDS; round++) {
        if (!remaining) {
            memset(selected, 0, sizeof(selected));
            remaining = available;
            if (available > 1) {
                selected[model->run.deck[round - 1]] = true;
                remaining--;
            }
        }
        unsigned total = 0;
        for (unsigned i = 0; i < STENO_SQUID_WORD_COUNT; i++) {
            if (!selected[i] && (!eligible || eligible[i])) total += steno_squid_word_weight(&model->record, i);
        }
        unsigned choice = ((uint64_t)random_word() * total) >> 32;
        for (unsigned i = 0; i < STENO_SQUID_WORD_COUNT; i++) {
            if (selected[i] || (eligible && !eligible[i])) continue;
            const unsigned weight = steno_squid_word_weight(&model->record, i);
            if (choice < weight) {
                selected[i] = true;
                remaining--;
                model->run.deck[round] = i;
                break;
            }
            choice -= weight;
        }
    }
}

static inline void steno_squid_model_begin(struct steno_squid_practice_model *model,
                                          uint32_t (*random_word)(void)) {
    memset(&model->run, 0, sizeof(model->run));
    model->run.review = STENO_SQUID_NO_REVIEW;
    model->run.active = true;
    model->run.limit = STENO_SQUID_WORD_ROUNDS;
    if (model->record.session < UINT32_MAX) model->record.session++;
    steno_squid_model_deck(model, NULL, STENO_SQUID_WORD_COUNT, random_word);
}

static inline bool steno_squid_model_trouble_begin(struct steno_squid_practice_model *model,
                                                 uint32_t (*random_word)(void)) {
    memset(&model->run, 0, sizeof(model->run));
    model->run.review = STENO_SQUID_NO_REVIEW;
    bool eligible[STENO_SQUID_WORD_COUNT];
    unsigned count = 0;
    for (unsigned i = 0; i < STENO_SQUID_WORD_COUNT; i++) {
        eligible[i] = steno_squid_word_trouble(&model->record.words[i]);
        count += eligible[i];
    }
    if (!count) return false;
    model->run.active = true;
    model->run.limit = STENO_SQUID_WORD_ROUNDS;
    if (model->record.session < UINT32_MAX) model->record.session++;
    steno_squid_model_deck(model, eligible, count, random_word);
    return true;
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
    model->run.limit = STENO_SQUID_HAND_ROUNDS;
}

static inline void steno_squid_model_multi_begin(struct steno_squid_practice_model *model,
                                                uint32_t (*random_word)(void)) {
    steno_squid_model_hands_begin(model);
    model->run.multi = true;
    model->run.limit = STENO_SQUID_WORD_ROUNDS;
    bool used[STENO_SQUID_MULTI_COUNT] = {false};
    for (unsigned i=0;i<STENO_SQUID_WORD_ROUNDS;i++) {
        unsigned choice = ((uint64_t)random_word() * (STENO_SQUID_MULTI_COUNT-i)) >> 32;
        for (unsigned j=0;j<STENO_SQUID_MULTI_COUNT;j++) {
            if (!used[j] && choice-- == 0) { used[j]=true;model->run.deck[i]=j;break; }
        }
    }
}

/* One XP for a correct key. Every fifth consecutive correct key adds a bonus
 * of 1, 2, 3, 4, then 5 XP (capped there). A clean 50-key run totals 110 XP
 * including completion, fitting the same bounded result packet as WORDS. */
static inline bool steno_squid_model_hands_answer(struct steno_squid_practice_model *model,
                                                 unsigned round, bool correct, uint32_t elapsed_ms) {
    struct steno_squid_word_run *run = &model->run;
    if (!run->active || !run->hand || run->finished || round != run->position || round >= run->limit) {
        return false;
    }
    if (!correct) {
        if (run->errors < 63) run->errors++;
        run->hand_streak = 0;
        if (run->multi) {
            if (run->word_errors < UINT8_MAX) run->word_errors++;
            if (run->word_errors > run->review_errors) {
                run->review_errors=run->word_errors;run->review=run->deck[round];
            }
        }
        return false; // Run feedback changed, but no persistent data changed.
    }
    run->hand_streak++;
    unsigned reward = 1;
    if (run->multi) {
        reward = run->word_errors ? 2 : 10;
        if (!run->word_errors) run->first_try++;
        run->word_errors = 0;
    } else if (run->hand_streak % 5 == 0) {
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
    if (!run->active || run->finished || run->position != run->limit) return false;
    run->finished = true;
    run->earned += 20;
    model->record.xp = model->record.xp > STENO_SQUID_XP_MAX - 20
                           ? STENO_SQUID_XP_MAX : model->record.xp + 20;
    if (model->record.rounds < UINT32_MAX) model->record.rounds++;
    return true;
}

/* A timed-out round keeps earned XP but gets no completion bonus. */
static inline bool steno_squid_model_timeout(struct steno_squid_practice_model *model) {
    if (!model->run.active || model->run.finished || model->run.hand ||
        model->run.position >= model->run.limit) return false;
    model->run.finished = true;
    if (model->record.rounds < UINT32_MAX) model->record.rounds++;
    return true;
}
