#pragma once

#include <stddef.h>
#include <stdint.h>

/* Display-only UTF-8 decoding and ASCII folding. These helpers never change
 * the dictionary text or the keystrokes emitted by Javelin. */
static inline uint32_t steno_squid_display_codepoint(const unsigned char **cursor) {
    const unsigned char *p = *cursor;
    const unsigned char first = *p++;
    unsigned continuation;
    uint32_t value, minimum;
    if (first < 0x80) {
        *cursor = p;
        return first;
    } else if (first >= 0xc2 && first <= 0xdf) {
        continuation = 1; value = first & 0x1f; minimum = 0x80;
    } else if (first >= 0xe0 && first <= 0xef) {
        continuation = 2; value = first & 0x0f; minimum = 0x800;
    } else if (first >= 0xf0 && first <= 0xf4) {
        continuation = 3; value = first & 7; minimum = 0x10000;
    } else {
        *cursor = p;
        return 0xfffd;
    }
    for (unsigned i = 0; i < continuation; i++) {
        if ((*p & 0xc0) != 0x80) {
            *cursor = p; /* Preserve any following ASCII; stop at a terminator. */
            return 0xfffd;
        }
        value = (value << 6) | (*p++ & 0x3f);
    }
    *cursor = p;
    return value < minimum || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff)
               ? 0xfffd : value;
}

/* Common decomposed Latin accents have no separate visible ASCII character. */
static inline int steno_squid_display_combining(uint32_t codepoint) {
    return codepoint >= 0x0300 && codepoint <= 0x036f;
}

/* One or two ASCII characters, or zero for a combining accent. */
static inline size_t steno_squid_display_ascii(uint32_t codepoint, char out[2]) {
    /* Latin base letters from Unicode 15.1.0 NFKD. */
    static const char latin[] =
        "AAAAAA?CEEEEIIII?NOOOOO??UUUUY??" /* U+00C0 */
        "aaaaaa?ceeeeiiii?nooooo??uuuuy?y" /* U+00E0 */
        "AaAaAaCcCcCcCcDd??EeEeEeEeEeGgGg" /* U+0100 */
        "GgGgHh??IiIiIiIiI???JjKk?LlLlLl?" /* U+0120 */
        "???NnNnNn???OoOoOo??RrRrRrSsSsSs" /* U+0140 */
        "SsTtTt??UuUuUuUuUuUuWwYyYZzZzZzs" /* U+0160 */
        "????????????????????????????????" /* U+0180 */
        "Oo?????????????Uu???????????????" /* U+01A0 */
        "?????????????AaIiOoUuUuUuUuUu?Aa" /* U+01C0 */
        "Aa????GgKkOoOo??j???Gg??NnAa????" /* U+01E0 */
        "AaAaEeEeIiIiOoOoRrRrUuUuSsTt??Hh" /* U+0200 */
        "??????AaEeOoOoOoOoYy????????????" /* U+0220 */
        "????????????????" /* U+0240 */
        ;
    /* Latin base letters from Unicode 15.1.0 NFKD. */
    static const char latin_additional[] =
        "AaBbBbBbCcDdDdDdDdDdEeEeEeEeEeFf" /* U+1E00 */
        "GgHhHhHhHhHhIiIiKkKkKkLlLlLlLlMm" /* U+1E20 */
        "MmMmNnNnNnNnOoOoOoOoPpPpRrRrRrRr" /* U+1E40 */
        "SsSsSsSsSsTtTtTtTtUuUuUuUuUuVvVv" /* U+1E60 */
        "WwWwWwWwWwXxXxYyZzZzZzhtwy?s????" /* U+1E80 */
        "AaAaAaAaAaAaAaAaAaAaAaAaEeEeEeEe" /* U+1EA0 */
        "EeEeEeEeIiIiOoOoOoOoOoOoOoOoOoOo" /* U+1EC0 */
        "OoOoUuUuUuUuUuUuUuYyYyYyYy??????" /* U+1EE0 */
        ;
    if (steno_squid_display_combining(codepoint)) {
        return 0;
    }
    switch (codepoint) {
    case 0x00c6: out[0] = 'A'; out[1] = 'E'; return 2;
    case 0x00e6: out[0] = 'a'; out[1] = 'e'; return 2;
    case 0x0152: out[0] = 'O'; out[1] = 'E'; return 2;
    case 0x0153: out[0] = 'o'; out[1] = 'e'; return 2;
    case 0x00df: out[0] = 's'; out[1] = 's'; return 2;
    case 0x1e9e: out[0] = 'S'; out[1] = 'S'; return 2;
    case 0x00de: out[0] = 'T'; out[1] = 'H'; return 2;
    case 0x00fe: out[0] = 't'; out[1] = 'h'; return 2;
    case 0x00d0: case 0x0110: out[0] = 'D'; return 1;
    case 0x00f0: case 0x0111: out[0] = 'd'; return 1;
    case 0x00d8: out[0] = 'O'; return 1;
    case 0x00f8: out[0] = 'o'; return 1;
    case 0x0141: out[0] = 'L'; return 1;
    case 0x0142: out[0] = 'l'; return 1;
    case 0x0131: out[0] = 'i'; return 1;
    case 0x00a0: case 0x202f: out[0] = ' '; return 1;
    case 0x2018: case 0x2019: case 0x201a: case 0x201b: out[0] = '\''; return 1;
    case 0x201c: case 0x201d: case 0x201e: case 0x201f: out[0] = '"'; return 1;
    case 0x2010: case 0x2011: case 0x2012: case 0x2013: case 0x2014:
    case 0x2212: out[0] = '-'; return 1;
    default:
        if (codepoint < 0x80) {
            out[0] = codepoint < 0x20 || codepoint == 0x7f ? ' ' : (char)codepoint;
        } else if (codepoint >= 0x00c0 && codepoint < 0x0250) {
            out[0] = latin[codepoint - 0x00c0];
        } else if (codepoint >= 0x1e00 && codepoint < 0x1f00) {
            out[0] = latin_additional[codepoint - 0x1e00];
        } else {
            out[0] = '?';
        }
        return 1;
    }
}
