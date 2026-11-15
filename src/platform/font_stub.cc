#include "font/monochrome/font.h"

// Javelin's button scripts can draw text with its built-in bitmap fonts. The
// ZMK build runs an empty button script and has no Javelin display, so these
// stand-ins replace the roughly 40 KB of glyph tables. font.cc still supplies
// the Font::FONTS table and the width helpers; with no glyphs, every character
// measures one pixel wide and nothing draws.
#define EMPTY_FONT {0, 0, 1, 1, 0, 0, nullptr, nullptr}

const Font Font::DEFAULT = EMPTY_FONT;
const Font Font::LARGE = EMPTY_FONT;
const Font Font::DOS = EMPTY_FONT;
const Font Font::SMALL_DIGITS = EMPTY_FONT;
const Font Font::MEDIUM_DIGITS = EMPTY_FONT;
const Font Font::LARGE_DIGITS = EMPTY_FONT;
const Font Font::HUGE_DIGITS = EMPTY_FONT;
