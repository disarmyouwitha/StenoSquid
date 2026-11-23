#include "button_script_manager.h"
#include "clock.h"
#include "dictionary/compact_map_dictionary.h"
#include "dictionary/dictionary_definition.h"
#include "dictionary/dictionary_list.h"
#include "engine.h"
#include "host_key_output.h"
#include "native_engine_backend.h"
#include "native_store_fixture.h"

#include <array>
#include <chrono>
#include <fstream>
#include <iostream>
#include <memory>
#include <sstream>
#include <thread>

namespace {
constexpr uint32_t Xip = 0x12000000;
using Timer = std::chrono::steady_clock;
using Nanoseconds = std::chrono::nanoseconds;

// Decode persisted 32-bit JSC4 pointers into native host pointers. The packed
// rows, hash tables, and text are read directly by the pinned Javelin code.
class Bases final : public candidate_b::NativeBaseProvider {
    struct Member {
        StenoCompactMapDictionaryDefinition definition{};
        std::array<StenoCompactMapDictionaryStrokesDefinition, 16> groups{};
        std::unique_ptr<StenoCompactMapDictionary> dictionary;
    };
    std::array<Member, 8> members;
    bool ready = true;

public:
    bool Ready() const override { return ready; }
    void Fault() override { ready = false; }

    size_t Load(uint32_t id, const uint8_t *image, const uint8_t *extent, unsigned index) {
        if (extent == nullptr) return 0;
        auto &member = members.at(id);
        const auto address = uint32_t(extent[0] | unsigned(extent[1]) << 8) * kSegmentBytes;
        const auto *collection = image + address;
        const auto xipPointer = [&](const uint8_t *word) {
            const uint32_t offset = Word(word) - Xip;
            assert(offset >= address && offset < address + Word(extent + 4));
            return image + offset;
        };
        const auto *map = xipPointer(collection + 36 + index * 4);
        auto &definition = member.definition;
        definition.maximumOutlineLength = map[1];
        definition.type = StenoDictionaryType::COMPACT_MAP;
        definition.name = reinterpret_cast<const char *>(xipPointer(map + 4));
        definition.textBlock = xipPointer(map + 8);
        definition.strokes = member.groups.data();
        const auto *table = xipPointer(map + 12);
        for (unsigned i = 0; i < definition.maximumOutlineLength; ++i, table += 12) {
            const auto mask = Word(table);
            if (mask) {
                member.groups.at(i) = {mask, xipPointer(table + 4),
                    reinterpret_cast<const StenoCompactHashMapEntryBlock *>(xipPointer(table + 8))};
            }
        }
        member.dictionary.reset(new (definition) StenoCompactMapDictionary(definition));
        return definition.maximumOutlineLength;
    }

    const StenoDictionary *FindBase(uint32_t id) const override {
        return id < members.size() ? members[id].dictionary.get() : nullptr;
    }
};

class CatalogDictionary final : public StenoDictionary {
public:
    CatalogDictionary(candidate_b::DictionaryBackend &backend, uint32_t id, std::string name, size_t max)
        : StenoDictionary(max), id(id), name(std::move(name)), overlay(backend, id, this->name.c_str()) {}
    const char *GetName() const override { return name.c_str(); }
    StenoDictionaryLookupResult Lookup(const StenoDictionaryLookup &query) const override {
        if (query.length <= maximumOutlineLength) return overlay.Lookup(query);
        return StenoDictionaryLookupResult::CreateInvalid();
    }
    const uint32_t id;
private:
    std::string name;
    candidate_b::NativeOverlayDictionary overlay;
};

void CheckProbes(const char *path, StenoDictionaryList &root) {
    std::ifstream probes(path);
    std::string outline, expected, owner;
    unsigned checks = 0;
    while (std::getline(probes, outline, '\t') && std::getline(probes, owner, '\t') &&
           std::getline(probes, expected)) {
        std::array<StenoStroke, 16> strokes;
        unsigned count = 0;
        std::istringstream keys(outline);
        std::string stroke;
        while (std::getline(keys, stroke, '/')) strokes.at(count++).Set(stroke.c_str());
        const StenoDictionaryLookup query(strokes.data(), count);
        auto result = root.Lookup(query);
        const auto *dict = static_cast<const CatalogDictionary *>(root.GetDictionaryForOutline(query));
        const auto id = std::stoul(owner);
        if ((dict ? dict->id : 0) != id || result.IsValid() != bool(id) ||
                (id && expected != result.GetText())) {
            std::cerr << "Lookup probe mismatch for " << outline << '\n';
            std::exit(1);
        }
        result.Destroy();
        ++checks;
    }
    assert(checks);
}

void Replay(const char *path, StenoDictionaryList &root, bool paced) {
    std::ifstream tape(path);
    StenoCompiledOrthography orthography(StenoOrthography::emptyOrthography);
    unsigned phase;
    while (tape >> phase) {
        size_t count;
        tape >> count;
        tape.ignore();
        alignas(StenoEngine) unsigned char memory[sizeof(StenoEngine)]{};
        auto &engine = *new (memory) StenoEngine(root, nullptr, orthography);
        Key::history.clear();
        HostKeyOutput editor;
        uint32_t virtualMs = 0;
        // Read each phase before starting the absolute native-clock schedule.
        struct Event { Nanoseconds deadline; StenoStroke stroke; std::string outline, expected; };
        std::vector<Event> events;
        for (size_t i = 0; i < count; ++i) {
            std::string deadline, outline, expected;
            if (!std::getline(tape, deadline, '\t') || !std::getline(tape, outline, '\t') ||
                    !std::getline(tape, expected)) std::exit(1);
            StenoStroke stroke;
            stroke.Set(outline.c_str());
            events.push_back({Nanoseconds(std::stoll(deadline)), stroke, outline, expected});
        }
        const auto start = Timer::now();
        Timer::time_point finished;
        for (size_t i = 0; i < events.size(); ++i) {
            const auto &event = events[i];
            const auto scheduled = start + event.deadline;
            if (paced) std::this_thread::sleep_until(scheduled);
            const auto milliseconds = uint32_t(std::chrono::duration_cast<std::chrono::milliseconds>(event.deadline).count());
            Clock::AdvanceMilliseconds(milliseconds - virtualMs);
            virtualMs = milliseconds;
            const auto begin = Timer::now();
            engine.Process(event.stroke);
            finished = Timer::now();
            editor.Consume();
            if (editor.text != event.expected) {
                std::cerr << phase << " WPM stroke " << i + 1 << " (" << event.outline
                          << "): expected [" << event.expected << "], got [" << editor.text << "]\n";
                std::exit(1);
            }
            const auto ns = [](auto elapsed) { return std::chrono::duration_cast<Nanoseconds>(elapsed).count(); };
            std::cout << phase << '\t' << i + 1 << '\t' << ns(finished - begin) << '\t'
                      << (paced ? ns(begin - scheduled) : 0) << '\t' << ns(finished - start) << '\n';
        }
        engine.~StenoEngine();
    }
}
} // namespace

int main(int argc, char **argv) {
    assert(argc == 5);
    alignas(4) static const uint8_t script[64] = {0x4a, 0x53, 0x53, 0x34};
    ButtonScriptManager::Initialize(script);
    std::ifstream pool(argv[1], std::ios::binary);
    NativeFixture storage;
    storage.nor.data.assign(std::istreambuf_iterator<char>(pool), {});
    assert(storage.nor.data.size() == 255 * kSegmentBytes);
    storage.nor.data.resize(256 * kSegmentBytes, 255);
    storage.Open();

    Bases bases;
    candidate_b::NativeEngineBackend<NativeDevice> backend(storage.store, bases);
    List<StenoDictionaryListEntry> entries;
    std::vector<std::unique_ptr<CatalogDictionary>> dictionaries;
    const auto *image = storage.nor.data.data();
    const auto *catalog = image + storage.store.selected.root.address;
    const unsigned rows = catalog[4] | unsigned(catalog[5]) << 8;
    for (unsigned i = 0; i < rows; ++i) {
        const auto *row = catalog + 32 + 96 * i;
        if (!Word(row + 4)) continue;
        const unsigned id = Word(row), base = row[8] | unsigned(row[9]) << 8;
        const auto *extent = base == 65535 ? nullptr : catalog + 32 + 96 * rows + 24 * base;
        const auto length = bases.Load(id, image, extent, row[10]);
        const size_t maximum = std::max<size_t>(length, storage.store.live.MaximumOutline(id));
        dictionaries.push_back(std::make_unique<CatalogDictionary>(backend, id,
            std::string(reinterpret_cast<const char *>(row + 16), row[12]), maximum));
        entries.Add(StenoDictionaryListEntry(dictionaries.back().get(), true));
    }
    StenoDictionaryList root(std::move(entries));
    CheckProbes(argv[2], root);
    assert(storage.store.Start(NativeStore<NativeDevice>::Work::Checkpoint) == JobResult::More);
    storage.Finish();
    storage.Open();
    root.OnLookupDataChanged();
    CheckProbes(argv[2], root);
    Replay(argv[3], root, std::string(argv[4]) == "paced");
    assert(bases.Ready());
}
