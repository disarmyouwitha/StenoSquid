#pragma once
#include "console.h"
#include "dictionary/dictionary.h"
#include "writer.h"

// A wake can recover an older valid catalog. The live collection must follow
// that catalog's flags and invalidate lookup caches before another stroke.
// Suppress unsolicited editor events while restoring persisted state.
template <class CellAt>
bool StenoSquidSyncNativeCollection(StenoDictionary &root, unsigned count, CellAt cell_at) {
    ConsoleWriter::Push(&NullWriter::instance);
    bool good = true;
    for (unsigned i = 0; i < count; ++i) {
        const auto &cell = cell_at(i);
        const bool accepted =
            cell.enabled ? root.EnableDictionary(cell.name) : root.DisableDictionary(cell.name);
        good = accepted && good;
    }
    ConsoleWriter::Pop();
    root.OnLookupDataChanged();
    return good;
}
