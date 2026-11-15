#pragma once

#include "host_layout.h"
#include "key_code.h"
#include "steno_key_code.h"

#include <stddef.h>
#include <stdint.h>

// Constant host layouts. HostLayout::ansi is defined in the engine's
// host_layout.cc, so another translation unit cannot use it at compile time and
// building a layout from it would run constructors and place the object in RAM.
// This header carries its own copy of the ASCII key codes (the pinned engine's
// table) so that each layout is constexpr data in flash.

namespace host_layout {

constexpr uint32_t SHIFT = MODIFIER_L_SHIFT_FLAG;

// Key code, with SHIFT, for each ASCII character: HostLayout::ansi.
constexpr uint16_t ascii_key_codes[128] = {
    // NUL       SOH           STX             ETX           EOT    ENQ    ACK    BEL
    0, 0, 0, 0, 0, 0, 0, 0,
    // BS        TAB           LF              VT            FF     CR     SO     SI
    KeyCode::BACKSPACE, KeyCode::TAB, KeyCode::ENTER, 0, 0, 0, 0, 0,
    // DLE       DC1           DC2             DC3           DC4    NAK    SYN    ETB
    0, 0, 0, 0, 0, 0, 0, 0,
    // CAN       EM            SUB             ESC           FS     GS     RS     US
    0, 0, 0, KeyCode::ESC, 0, 0, 0, 0,
    //              !               "               #               $               %               &               '
    KeyCode::SPACE, SHIFT|KeyCode::_1, SHIFT|KeyCode::APOSTROPHE,SHIFT|KeyCode::_3, SHIFT|KeyCode::_4, SHIFT|KeyCode::_5, SHIFT|KeyCode::_7, KeyCode::APOSTROPHE,
    // (            )               *               +               ,               -               .               /
    SHIFT|KeyCode::_9, SHIFT|KeyCode::_0, SHIFT|KeyCode::_8, SHIFT|KeyCode::EQUAL, KeyCode::COMMA, KeyCode::MINUS, KeyCode::DOT, KeyCode::SLASH,
    // 0            1               2               3               4               5               6               7
    KeyCode::_0, KeyCode::_1, KeyCode::_2, KeyCode::_3, KeyCode::_4, KeyCode::_5, KeyCode::_6, KeyCode::_7,
    // 8            9               :               ;               <               =               >               ?
    KeyCode::_8, KeyCode::_9, SHIFT|KeyCode::SEMICOLON,KeyCode::SEMICOLON, SHIFT|KeyCode::COMMA,KeyCode::EQUAL, SHIFT|KeyCode::DOT, SHIFT|KeyCode::SLASH,
    // @            A               B               C               D               E               F               G
    SHIFT|KeyCode::_2, SHIFT|KeyCode::A, SHIFT|KeyCode::B, SHIFT|KeyCode::C, SHIFT|KeyCode::D, SHIFT|KeyCode::E, SHIFT|KeyCode::F, SHIFT|KeyCode::G,
    // H            I               J               K               L               M               N               O
    SHIFT|KeyCode::H, SHIFT|KeyCode::I, SHIFT|KeyCode::J, SHIFT|KeyCode::K, SHIFT|KeyCode::L, SHIFT|KeyCode::M, SHIFT|KeyCode::N, SHIFT|KeyCode::O,
    // P            Q               R               S               T               U               V               W
    SHIFT|KeyCode::P, SHIFT|KeyCode::Q, SHIFT|KeyCode::R, SHIFT|KeyCode::S, SHIFT|KeyCode::T, SHIFT|KeyCode::U, SHIFT|KeyCode::V, SHIFT|KeyCode::W,
    // X            Y               Z               [               \               ]               ^               _
    SHIFT|KeyCode::X, SHIFT|KeyCode::Y, SHIFT|KeyCode::Z, KeyCode::L_BRACKET, KeyCode::BACKSLASH, KeyCode::R_BRACKET, SHIFT|KeyCode::_6, SHIFT|KeyCode::MINUS,
    // `            a               b               c               d               e               f               g
    KeyCode::GRAVE, KeyCode::A, KeyCode::B, KeyCode::C, KeyCode::D, KeyCode::E, KeyCode::F, KeyCode::G,
    // h            i               j               k               l               m               n               o
    KeyCode::H, KeyCode::I, KeyCode::J, KeyCode::K, KeyCode::L, KeyCode::M, KeyCode::N, KeyCode::O,
    // p            q               r               s               t               u               v               w
    KeyCode::P, KeyCode::Q, KeyCode::R, KeyCode::S, KeyCode::T, KeyCode::U, KeyCode::V, KeyCode::W,
    // x            y               z               {               |               }               ~               DEL
    KeyCode::X, KeyCode::Y, KeyCode::Z, SHIFT|KeyCode::L_BRACKET,SHIFT|KeyCode::BACKSLASH,SHIFT|KeyCode::R_BRACKET,SHIFT|KeyCode::GRAVE, 0,
};

// HostLayout stores its entries inline after the header, because the list type
// has zero capacity. This mirrors that layout with the entries in place, so a
// constant instance can be used wherever the engine expects a HostLayout.
template <size_t N> struct Const {
  char name[30];
  uint16_t scriptOffset;
  uint16_t asciiKeyCodes[128];
  size_t entryCount;
  HostLayoutEntry entries[N];

  // fill(entries, ascii_key_codes) writes the N sorted entries.
  template <typename Fill>
  constexpr explicit Const(Fill fill)
      : name{'u', 's'}, scriptOffset(0), asciiKeyCodes{}, entryCount(N), entries{} {
    for (size_t i = 0; i < 128; ++i) {
      asciiKeyCodes[i] = ascii_key_codes[i];
    }
    fill(entries, asciiKeyCodes);
  }

  const HostLayout &layout() const { return *reinterpret_cast<const HostLayout *>(this); }
};

static_assert(offsetof(Const<1>, name) == offsetof(HostLayout, name));
static_assert(offsetof(Const<1>, scriptOffset) == offsetof(HostLayout, scriptOffset));
static_assert(offsetof(Const<1>, asciiKeyCodes) == offsetof(HostLayout, asciiKeyCodes));
static_assert(offsetof(Const<1>, entryCount) == offsetof(HostLayout, entries));
static_assert(offsetof(Const<1>, entries) == sizeof(HostLayout));

} // namespace host_layout
