#include "screen.h"

#include <stdio.h>

#include "../practice_types.h"
#include "../practice_menu.h"
#include <string.h>

static lv_obj_t *welcome_page;
static lv_obj_t *run_page;
#define PRACTICE_MENU_ROWS 3
static lv_obj_t *drill_choices[PRACTICE_MENU_ROWS];
static lv_obj_t *navigation_label;
static lv_obj_t *start_label;
static unsigned menu_first;
static const char *const choices[] = {"WORDS 10", "LEFT 30", "RIGHT 30", "COLUMNS", "Build-Up", "Multi-", "SPEED", "TROUBLE"};
BUILD_ASSERT(sizeof(choices) / sizeof(choices[0]) == STENO_SQUID_PRACTICE_DRILL_COUNT);
static lv_obj_t *drill_label;
static lv_obj_t *drill_separator;
static lv_obj_t *prompt_label;
static lv_obj_t *outline_label;
static lv_obj_t *outline_second;
static lv_obj_t *feedback_label;
static lv_obj_t *progress_bar;
static lv_obj_t *progress_label;
static lv_obj_t *best_label;
static uint32_t last_practice;
static uint64_t last_progress;
static struct steno_squid_practice_view last_view;
static bool view_dirty;
static unsigned result_card;
static lv_timer_t *results_timer;

static void fit_text(lv_obj_t *label, const char *text, bool large) {
    static const lv_font_t *const large_fonts[] = {
        &lv_font_montserrat_28, &lv_font_montserrat_20, &lv_font_montserrat_16,
        &lv_font_montserrat_14, &lv_font_montserrat_12,
        &lv_font_montserrat_10, &lv_font_montserrat_8};
    static const lv_font_t *const small_fonts[] = {
        &lv_font_montserrat_12, &lv_font_montserrat_10, &lv_font_montserrat_8};
    const lv_font_t *const *fonts = large ? large_fonts : small_fonts;
    const unsigned count = large ? sizeof(large_fonts) / sizeof(large_fonts[0])
                                 : sizeof(small_fonts) / sizeof(small_fonts[0]);
    const lv_font_t *font = fonts[count - 1];
    for (unsigned i = 0; i < count; i++) {
        if (lv_text_get_width(text, strlen(text), fonts[i], 0) <= 66) {
            font = fonts[i];
            break;
        }
    }
    lv_obj_set_style_text_font(label, font, 0);
    lv_label_set_long_mode(label, LV_LABEL_LONG_CLIP);
    lv_label_set_text(label, text);
}

/* The footer sits just above the battery, leaving a taller outline area. */
static void fit_outline(lv_obj_t *label, const char *text) {
    static const lv_font_t *const fonts[] = {
        &lv_font_montserrat_20, &lv_font_montserrat_16, &lv_font_montserrat_14};
    fit_text(label, text, false);
    for (unsigned i=0; i<sizeof(fonts)/sizeof(fonts[0]); i++) {
        if (lv_text_get_width(text, strlen(text), fonts[i], 0) <= 66) {
            lv_obj_set_style_text_font(label, fonts[i], 0);
            break;
        }
    }
}

/* Left thumb vowels A/O stay above the right hand's E/U and consonants.
 * Canonical outlines use a hyphen when no vowel separates the hands. */
static const char *outline_hand_break(const char *outline) {
    const char *middle = strpbrk(outline, "AO*EU-");
    if (!middle || (middle == outline && strchr("EU-", *middle))) {
        return NULL;
    }
    const char *right = middle + strspn(middle, "AO*");
    if (*right == '-') {
        ++right;
    }
    return right > outline && *right ? right : NULL;
}

static void fit_multi_text(lv_obj_t *label, const char *text, bool stroke) {
    static const lv_font_t *const fonts[] = {&lv_font_montserrat_20, &lv_font_montserrat_16, &lv_font_montserrat_14};
    fit_text(label, text, false);
    for (unsigned i=stroke ? 0 : 1; i<sizeof(fonts)/sizeof(fonts[0]); i++) {
        if (lv_text_get_width(text, strlen(text), fonts[i], 0) <= 66) {
            lv_obj_set_style_text_font(label, fonts[i], 0);
            break;
        }
    }
}

static void progress_labels(void) {
    if (!best_label) return;
    char text[32];
    unsigned into, needed;
    const unsigned level = steno_squid_pop_level(steno_squid_practice_xp(last_progress), &into, &needed);
    const unsigned drill = (last_practice & STENO_SQUID_PRACTICE_DRILL_MASK) >> STENO_SQUID_PRACTICE_DRILL_SHIFT;
    const bool words = steno_squid_practice_is_word_like(drill);
    const bool speed = drill == STENO_SQUID_PRACTICE_SPEED;
    const bool timeout = last_practice & STENO_SQUID_PRACTICE_TIMEOUT;
    if (steno_squid_practice_feedback_value(last_practice) != STENO_SQUID_PRACTICE_DONE) return;
    if (speed) snprintf(text,sizeof(text),"%u/%u",steno_squid_practice_counter(last_practice,STENO_SQUID_PRACTICE_ROUND_SHIFT),STENO_SQUID_SPEED_ROUNDS);
    else if (words) snprintf(text, sizeof(text), "%u/10", steno_squid_practice_first_try(last_progress));
    else snprintf(text, sizeof(text), "%u/%u", STENO_SQUID_HAND_ROUNDS, STENO_SQUID_HAND_ROUNDS);
    fit_text(prompt_label, text, true);
    lv_obj_set_style_text_font(prompt_label, &lv_font_montserrat_20, 0);
    snprintf(text, sizeof(text), "Lv %u", level);
    fit_text(progress_label, text, true);
    lv_obj_set_style_text_font(progress_label,
        lv_text_get_width(text, strlen(text), &lv_font_montserrat_20, 0) <= 66
            ? &lv_font_montserrat_20 : &lv_font_montserrat_16, 0);
    bool review_word = false;
    const unsigned average = steno_squid_practice_average(last_progress);
    switch (result_card % 4) {
    case 0:
        if (timeout) snprintf(text,sizeof(text),"Time up");
        else snprintf(text, sizeof(text), "+%u XP", steno_squid_practice_xp_earned(last_progress));
        break;
    case 1:
        if (timeout) { snprintf(text,sizeof(text),"+%u XP",steno_squid_practice_xp_earned(last_progress));break; }
        if (!words) {
            snprintf(text, sizeof(text), "Best %u", steno_squid_practice_counter(last_practice, STENO_SQUID_PRACTICE_BEST_SHIFT));
        } else if (average == 511) snprintf(text, sizeof(text), "Avg 51s+");
        else snprintf(text, sizeof(text), "Avg %u.%us", average / 10, average % 10);
        break;
    case 2: {
        if (speed) {
            snprintf(text,sizeof(text),"Best %u",steno_squid_practice_counter(last_practice,STENO_SQUID_PRACTICE_BEST_SHIFT));
            break;
        }
        if (!words) {
            if (average == 511) snprintf(text, sizeof(text), "Avg 51s+");
            else snprintf(text, sizeof(text), "Avg %u.%us", average / 10, average % 10);
            break;
        }
        if (last_view.review[0] && steno_squid_practice_errors(last_progress)) {
            fit_text(progress_label, "Review", false);
            snprintf(text, sizeof(text), "%s", last_view.review);
            review_word = true;
        } else snprintf(text, sizeof(text), "Clean run!");
        break;
    }
    default: snprintf(text, sizeof(text), "%u/%u XP", into, needed); break;
    }
    fit_text(best_label, text, false);
    if (review_word && lv_text_get_width(text, strlen(text), &lv_font_montserrat_14, 0) <= 66) {
        lv_obj_set_style_text_font(best_label, &lv_font_montserrat_14, 0);
    }
}

static void start_instruction(void) {
    if (!start_label) return;
    const unsigned drill = (last_practice & STENO_SQUID_PRACTICE_DRILL_MASK) >> STENO_SQUID_PRACTICE_DRILL_SHIFT;
    fit_text(start_label, drill == STENO_SQUID_PRACTICE_TROUBLE && steno_squid_practice_no_trouble(last_progress)
                              ? "Try Words" : "A/O: GO", false);
}

static void steno_squid_practice_widget_set_progress(uint64_t progress) {
    if (progress == last_progress) return;
    last_progress = progress;
    result_card = 0;
    if (results_timer && !lv_timer_get_paused(results_timer)) lv_timer_reset(results_timer);
    progress_labels();
    if (last_practice & STENO_SQUID_PRACTICE_MENU) start_instruction();
}

void steno_squid_practice_widget_set_view(const struct steno_squid_practice_view *view) {
    const bool changed = steno_squid_practice_seconds(view->progress) != steno_squid_practice_seconds(last_view.progress) ||
                         strcmp(view->prompt, last_view.prompt) ||
                         strcmp(view->outline, last_view.outline) ||
                         strcmp(view->review, last_view.review);
    last_view = *view;
    view_dirty |= changed;
    steno_squid_practice_widget_set_progress(view->progress);
}

void steno_squid_practice_widget_hide(void) {
    if (results_timer) lv_timer_pause(results_timer);
    last_practice = 0;
}

static void result_tick(lv_timer_t *timer) {
    if ((last_practice & STENO_SQUID_PRACTICE_ACTIVE) &&
        !(last_practice & STENO_SQUID_PRACTICE_MENU) &&
        steno_squid_practice_feedback_value(last_practice) == STENO_SQUID_PRACTICE_DONE) {
        result_card++;
        progress_labels();
    }
}

static lv_obj_t *menu_separator(lv_obj_t *parent, int y) {
    lv_obj_t *line = lv_obj_create(parent);
    lv_obj_remove_style_all(line);
    lv_obj_remove_flag(line, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_pos(line, 4, y);
    lv_obj_set_size(line, 60, 1);
    lv_obj_set_style_bg_color(line, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(line, LV_OPA_COVER, 0);
    return line;
}

lv_obj_t *steno_squid_practice_widget_create(lv_obj_t *parent) {
    lv_obj_t *page = lv_obj_create(parent);
    lv_obj_remove_style_all(page);
    lv_obj_remove_flag(page, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_pos(page, 0, 10);
    lv_obj_set_size(page, 68, 130);

    welcome_page = lv_obj_create(page);
    lv_obj_remove_style_all(welcome_page);
    lv_obj_remove_flag(welcome_page, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_size(welcome_page, 68, 130);
    lv_obj_t *welcome = steno_squid_display_label(welcome_page, 0, &lv_font_montserrat_12);
    lv_label_set_text_static(welcome, "Practice");
    menu_separator(welcome_page, 19);
    for (unsigned i = 0; i < PRACTICE_MENU_ROWS; ++i) {
        drill_choices[i] = steno_squid_display_label(welcome_page, 23 + i * 23,
                                                &lv_font_montserrat_12);
        lv_obj_set_x(drill_choices[i], 1);
        lv_obj_set_width(drill_choices[i], 66);
        lv_obj_set_style_pad_ver(drill_choices[i], 3, 0);
        lv_obj_set_style_bg_opa(drill_choices[i], LV_OPA_COVER, 0);
        lv_label_set_text_static(drill_choices[i], choices[i]);
    }
    menu_separator(welcome_page, 92);
    navigation_label = steno_squid_display_label(welcome_page, 98, &lv_font_unscii_8);
    lv_label_set_text_static(navigation_label, "*- / -*");
    start_label = steno_squid_display_label(welcome_page, 115, &lv_font_unscii_8);
    lv_label_set_text_static(start_label, "A/O: GO");

    run_page = lv_obj_create(page);
    lv_obj_remove_style_all(run_page);
    lv_obj_remove_flag(run_page, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_size(run_page, 68, 130);

    drill_label = steno_squid_display_label(run_page, 0, &lv_font_montserrat_12);
    drill_separator = menu_separator(run_page, 16);
    prompt_label = steno_squid_display_label(run_page, 19, &lv_font_montserrat_28);
    outline_label = steno_squid_display_label(run_page, 58, &lv_font_montserrat_12);
    outline_second = steno_squid_display_label(run_page, 62, &lv_font_montserrat_12);
    lv_obj_add_flag(outline_second, LV_OBJ_FLAG_HIDDEN);
    feedback_label = steno_squid_display_label(run_page, 88, &lv_font_unscii_8);
    lv_obj_set_x(feedback_label, 1);
    lv_obj_set_width(feedback_label, 66);
    lv_obj_set_style_pad_ver(feedback_label, 2, 0);
    lv_obj_set_style_text_color(feedback_label, lv_color_white(), 0);
    lv_obj_set_style_bg_color(feedback_label, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(feedback_label, LV_OPA_COVER, 0);

    progress_bar = steno_squid_display_bar(run_page, 4, 103, 60, 8);
    progress_label = steno_squid_display_label(run_page, 115, &lv_font_montserrat_8);
    best_label = steno_squid_display_label(run_page, 113, &lv_font_montserrat_12);
    progress_labels();
    results_timer = lv_timer_create(result_tick, 2500, NULL);
    lv_timer_pause(results_timer);
    return page;
}

void steno_squid_practice_widget_update(uint32_t practice) {
    static const char *const drill_names[] = {"WORDS", "LEFT", "RIGHT", "COLUMNS", "Build-Up", "Multi-", "SPEED", "TROUBLE"};
    static const char *const feedback_names[] = {"READY", "CORRECT", "RETRY"};
    if (last_practice == practice && !view_dirty) {
        return;
    }
    const unsigned feedback = steno_squid_practice_feedback_value(practice);
    last_practice = practice;
    view_dirty = false;
    const unsigned round = steno_squid_practice_counter(practice, STENO_SQUID_PRACTICE_ROUND_SHIFT);
    const unsigned streak = steno_squid_practice_counter(practice, STENO_SQUID_PRACTICE_STREAK_SHIFT);
    const unsigned best = steno_squid_practice_counter(practice, STENO_SQUID_PRACTICE_BEST_SHIFT);
    const enum steno_squid_practice_drill drill =
        (practice & STENO_SQUID_PRACTICE_DRILL_MASK) >> STENO_SQUID_PRACTICE_DRILL_SHIFT;
    const bool menu = (practice & STENO_SQUID_PRACTICE_MENU) != 0;
    if (menu) {
        lv_timer_pause(results_timer);
        lv_obj_remove_flag(welcome_page, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(run_page, LV_OBJ_FLAG_HIDDEN);
        menu_first = steno_squid_menu_first(menu_first, drill, STENO_SQUID_PRACTICE_DRILL_COUNT, PRACTICE_MENU_ROWS);
        for (unsigned i = 0; i < PRACTICE_MENU_ROWS; ++i) {
            const unsigned item = menu_first + i;
            if (item >= STENO_SQUID_PRACTICE_DRILL_COUNT) {
                lv_obj_add_flag(drill_choices[i], LV_OBJ_FLAG_HIDDEN);
                continue;
            }
            lv_obj_remove_flag(drill_choices[i], LV_OBJ_FLAG_HIDDEN);
            lv_label_set_text_static(drill_choices[i], choices[item]);
            const bool selected = item == drill;
            lv_obj_set_style_bg_color(drill_choices[i],
                                      selected ? lv_color_black() : lv_color_white(), 0);
            lv_obj_set_style_text_color(drill_choices[i],
                                        selected ? lv_color_white() : lv_color_black(), 0);
        }
        if (STENO_SQUID_PRACTICE_DRILL_COUNT > PRACTICE_MENU_ROWS) {
            char navigation[10];
            snprintf(navigation, sizeof(navigation), "%c*-/-*%c", menu_first ? '^' : ' ',
                     menu_first + PRACTICE_MENU_ROWS < STENO_SQUID_PRACTICE_DRILL_COUNT ? 'v' : ' ');
            lv_label_set_text(navigation_label, navigation);
        }
        start_instruction();
        return;
    }
    lv_obj_add_flag(welcome_page, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(run_page, LV_OBJ_FLAG_HIDDEN);
    const unsigned total = steno_squid_practice_rounds(drill);
    const bool done = feedback == STENO_SQUID_PRACTICE_DONE;
    if (done) { lv_timer_reset(results_timer); lv_timer_resume(results_timer); }
    else lv_timer_pause(results_timer);
    const bool no_dictionary = (practice & STENO_SQUID_PRACTICE_NO_DICTIONARY) != 0;
    const unsigned build_stage = (practice & STENO_SQUID_PRACTICE_BUILD_MASK) >> STENO_SQUID_PRACTICE_BUILD_SHIFT;
    const unsigned completed = round + (feedback == STENO_SQUID_PRACTICE_CORRECT &&
        (drill != STENO_SQUID_PRACTICE_BUILD || build_stage == 2));
    const char *outline = last_view.outline[0] ? last_view.outline : "...";
    const char *prompt = last_view.prompt[0] ? last_view.prompt : "...";
    char progress[32];

    lv_label_set_text_static(drill_label, done ? "A: MENU" : drill_names[drill]);
    lv_obj_set_style_text_decor(drill_label, done ? LV_TEXT_DECOR_UNDERLINE : LV_TEXT_DECOR_NONE, 0);
    lv_obj_set_style_text_font(prompt_label, done ? &lv_font_montserrat_16 : &lv_font_montserrat_28, 0);
    fit_text(prompt_label, done ? "DONE" : prompt, !done);
    lv_obj_set_y(outline_label, 58);
    lv_obj_set_y(outline_second, 62);
    lv_obj_set_style_text_decor(outline_label, LV_TEXT_DECOR_NONE, 0);
    lv_obj_set_style_text_decor(outline_second, LV_TEXT_DECOR_NONE, 0);
    lv_obj_add_flag(outline_second, LV_OBJ_FLAG_HIDDEN);
    const char *slash = drill == STENO_SQUID_PRACTICE_MULTI ? strchr(outline, '/') : NULL;
    if (!done && !no_dictionary && slash) {
        char first[32], second[32];
        snprintf(first,sizeof(first),"%.*s",(int)(slash-outline),outline);
        snprintf(second,sizeof(second),"%s",slash+1);
        fit_multi_text(prompt_label,prompt,false); // Reserve two 20 px stroke rows.
        lv_obj_set_y(outline_label,38);
        fit_multi_text(outline_label,first,true);
        fit_multi_text(outline_second,second,true);
        lv_obj_remove_flag(outline_second,LV_OBJ_FLAG_HIDDEN);
        if (feedback != STENO_SQUID_PRACTICE_CORRECT) {
            lv_obj_set_style_text_decor((practice & STENO_SQUID_PRACTICE_PARTIAL) ? outline_second : outline_label,
                                       LV_TEXT_DECOR_UNDERLINE,0);
        }
    } else if (!done && !no_dictionary &&
               (drill == STENO_SQUID_PRACTICE_WORDS ||
                drill == STENO_SQUID_PRACTICE_TROUBLE ||
                drill == STENO_SQUID_PRACTICE_SPEED) &&
               lv_text_get_width(outline, strlen(outline), &lv_font_montserrat_16, 0) > 66 &&
               outline_hand_break(outline)) {
        const char *right = outline_hand_break(outline);
        size_t left_size = (size_t)(right - outline);
        if (outline[left_size - 1] == '-') {
            --left_size;
        }
        char left[sizeof(last_view.outline)];
        snprintf(left, sizeof(left), "%.*s", (int)left_size, outline);
        fit_outline(prompt_label, prompt);
        fit_outline(outline_label, left);
        fit_outline(outline_second, right);
        lv_obj_set_y(outline_label, 42);
        lv_obj_set_y(outline_second, 64);
        lv_obj_remove_flag(outline_second, LV_OBJ_FLAG_HIDDEN);
    } else {
        fit_outline(outline_label, no_dictionary ? "Enable dict" : outline);
    }
    lv_obj_set_y(progress_label, done ? 91 : 115);
    lv_obj_set_style_text_font(progress_label,
                               done ? &lv_font_montserrat_16 : &lv_font_montserrat_8, 0);
    lv_obj_set_y(prompt_label, done ? 15 : (slash && !no_dictionary ? 18 : 19));
    if (done) {
        lv_obj_add_flag(drill_separator, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(outline_label, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(feedback_label, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(progress_bar, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(best_label, LV_OBJ_FLAG_HIDDEN);
        progress[0] = 0;
    } else {
        lv_obj_remove_flag(drill_separator, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(outline_label, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(feedback_label, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(progress_bar, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(best_label, LV_OBJ_FLAG_HIDDEN);
        const char *feedback_text = feedback_names[feedback];
        if (drill == STENO_SQUID_PRACTICE_MULTI) {
            if (feedback == STENO_SQUID_PRACTICE_PROMPT) {
                feedback_text = (practice & STENO_SQUID_PRACTICE_PARTIAL) ? "STROKE 2" : "STROKE 1";
            } else if (feedback == STENO_SQUID_PRACTICE_INCORRECT) {
                feedback_text = "RETRY 1";
            }
        }
        lv_label_set_text_static(feedback_label, no_dictionary ? "DICT OFF" : feedback_text);
        lv_bar_set_range(progress_bar, 0, total);
        lv_bar_set_value(progress_bar, completed, LV_ANIM_OFF);
        if (drill == STENO_SQUID_PRACTICE_SPEED) {
            snprintf(progress,sizeof(progress),"%u/%u  %us",round,total,steno_squid_practice_seconds(last_progress));
        } else snprintf(progress, sizeof(progress), "%u/%u  Streak %u", round + 1, total, streak);
    }
    if (!done && drill == STENO_SQUID_PRACTICE_SPEED) fit_text(progress_label,progress,false);
    else lv_label_set_text(progress_label, progress);
    if (done) { result_card = 0; progress_labels(); }
}
