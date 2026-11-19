#pragma once
#include "../../tools/storage/native_store.h"
namespace candidate_b {
// Same-boot idle resume only. The caller must retain healthy engine/mapper RAM,
// hold the flash mutex, and have restored quad/DMA access. This checks catalog
// identity/CRC, not every retained payload byte; cold boot still fully validates.
template <class Device> bool NativeWarmResumeEligible(NativeStore<Device> &store) {
    if (!store.opened || !store.live.Ready() || store.work != NativeStore<Device>::Work::None ||
        store.selected.quarantine || !store.catalog.CanWrite(store.selected.root))
        return false;
    const auto &live = store.live.Root();
    const auto &selected = store.selected.root;
    if (live.generation != selected.generation || live.address != selected.address ||
        live.bytes != selected.bytes || live.crc != selected.crc)
        return false;
    // Export/lookup leases normally prevent sleep. Refuse a shortcut if a caller
    // has nevertheless retained one, rather than changing its trust underneath it.
    if (store.catalog.VisitPins([](const CatalogRoot &) { return false; }) != CatalogResult::Ok)
        return false;
    for (unsigned slot = 0; slot < 2; ++slot) {
        CatalogRoot observed;
        const auto status = store.catalog.Inspect(slot, observed);
        if (status == CatalogResult::Empty && slot != store.selected.slot)
            continue;
        if (status != CatalogResult::Ok || !store.catalog.IsTrusted(observed))
            return false;
        if (slot == store.selected.slot &&
            (observed.generation != selected.generation || observed.address != selected.address ||
             observed.bytes != selected.bytes || observed.crc != selected.crc))
            return false;
    }
    return true;
}
} // namespace candidate_b
