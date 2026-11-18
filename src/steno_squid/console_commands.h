#pragma once
#include <string.h>

static inline bool steno_squid_console_command_allowed(const char *line, bool usb) {
    struct Allowed { const char *name; bool usb_only; };
    static constexpr Allowed commands[] = {
#define UPSTREAM_COMMAND(name) {name, false},
#define ENGINE_COMMAND(name, help, handler) {name, false},
#define USER_COMMAND(name, help, handler, usb_only) {name, usb_only},
#include "console_commands.def"
#undef UPSTREAM_COMMAND
#undef ENGINE_COMMAND
#undef USER_COMMAND
    };
    for (const auto &command : commands) {
        const size_t n = strlen(command.name);
        if ((!command.usb_only || usb) && strncmp(line, command.name, n) == 0 &&
            (line[n] == ' ' || line[n] == '\n' || !line[n])) return true;
    }
    return strcmp(line, "print_dictionary user_dictionary\n") == 0 ||
           strcmp(line, "enable_events dictionary_status\n") == 0 ||
           strcmp(line, "disable_events dictionary_status\n") == 0;
}
