#pragma once
#include "key.h"
#include <string>

// Apply the engine's captured HID key events to a small ASCII editor model.
class HostKeyOutput {
public:
    std::string text;

    void Consume() {
        for (; next < Key::history.size(); ++next) {
            const auto &event = Key::history[next];
            const auto code = event.code.value;
            if (code >= KeyCode::L_CTRL && code <= KeyCode::R_META) {
                const uint32_t bit = 1u << (8 + code - KeyCode::L_CTRL);
                if (event.isPress) modifiers |= bit;
                else modifiers &= ~bit;
            } else if (event.isPress) {
                if (code == KeyCode::BACKSPACE) {
                    if (!text.empty()) text.pop_back();
                } else {
                    const auto ch = KeyCode::ConvertToUnicode(code | modifiers);
                    if (ch && ch < 128) text += char(ch);
                }
            }
        }
    }

private:
    size_t next = 0;
    uint32_t modifiers = 0;
};
