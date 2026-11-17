#pragma once
#include <lvgl.h>
#include <stddef.h>
#include <string.h>

/* Leave one pixel for the extra bold pass on the 68-pixel panel. */
#define STENO_SQUID_TEXT_WIDTH 66
static inline const lv_font_t *steno_squid_fit_font(const char *text, size_t length,
        const lv_font_t *const *fonts, size_t count, unsigned width) {
    for (size_t i = 0; i < count; ++i) {
        if (lv_text_get_width(text, length, fonts[i], 0) <= (int32_t)width) return fonts[i];
    }
    return NULL;
}
static inline void steno_squid_set_label(lv_obj_t *label, const char *text, const lv_font_t *font) {
    if (font && lv_obj_get_style_text_font(label, LV_PART_MAIN) != font)
        lv_obj_set_style_text_font(label, font, 0);
    if (strcmp(lv_label_get_text(label), text) != 0) lv_label_set_text(label, text);
}
