#pragma once

/* Keep a selection inside a fixed-height menu window. Navigation itself wraps
 * at the drill count; this helper scrolls the visible rows to follow it. */
static inline unsigned steno_squid_menu_first(unsigned first, unsigned selected,
                                             unsigned count, unsigned rows) {
    if (!rows || count <= rows) return 0;
    if (selected >= count) selected = count - 1;
    if (selected < first) first = selected;
    if (selected >= first + rows) first = selected - rows + 1;
    return first > count - rows ? count - rows : first;
}
