#pragma once
#include "console.h"
#include "javelin_hid_transport.h"
#include "dictionary_flash.h"
#include <string.h>

/* Used only inside the HID console's engine lock and root writer scope.
 * Formatting is protected, but a blocked USB/BLE send never holds that lock.
 * The fixed buffer avoids a large JSON allocation for long translations. */
class StenoSquidExportWriter final : public IWriter {
public:
    explicit StenoSquidExportWriter(bool allow_yield)
        : output(ConsoleWriter::GetActiveWriter()), allow_yield(allow_yield) {
        ConsoleWriter::Push(this);
    }
    ~StenoSquidExportWriter() {
        Flush();
        ConsoleWriter::Pop();
        if (failed) output->Write("ERR Flash unavailable during export\n", 36);
    }
    void Write(const char *text, size_t length) override {
        while (length && !failed) {
            const size_t count = length < sizeof(buffer) - used ? length : sizeof(buffer) - used;
            memcpy(buffer + used, text, count);
            used += count; text += count; length -= count;
            if (used == sizeof(buffer)) Flush();
        }
    }
    void Flush() {
        if (!used || failed) return;
        if (allow_yield) {
            ConsoleWriter::Pop(); // This formatter.
            ConsoleWriter::Pop(); // The HID console's root writer.
            dictionary_flash_release();
        }
        output->Write(buffer, used);
        used = 0;
        if (allow_yield) {
            failed = !dictionary_flash_resume_read(); // Always returns with the lock held.
            ConsoleWriter::Push(output);
            ConsoleWriter::Push(this);
        }
    }
    bool Failed() const { return failed; }
private:
    IWriter *output;
    bool allow_yield;
    bool failed = false;
    char buffer[JAVELIN_HID_REPORT_BYTES];
    size_t used = 0;
};
