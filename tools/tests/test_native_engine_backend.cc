#include "native_engine_backend.h"
#include "native_store_fixture.h"
#include <iostream>
#include <vector>
struct Bases : candidate_b::NativeBaseProvider {
    struct BaseDictionary final : StenoDictionary {
        mutable unsigned calls=0;
        explicit BaseDictionary(size_t limit):StenoDictionary(limit){}
        const char *GetName()const override{return "compact base";}
        StenoDictionaryLookupResult Lookup(const StenoDictionaryLookup &lookup)const override {
            assert(lookup.length && lookup.length<=GetMaximumOutlineLength());
            ++calls;char *text=static_cast<char *>(std::malloc(4));std::memcpy(text,"cat",4);
            return StenoDictionaryLookupResult::CreateDynamicString(text);
        }
    } base;
    bool ready=true;
    explicit Bases(size_t limit=1):base(limit){}
    bool Ready()const override{return ready;}
    void Fault()override{ready=false;}
    const StenoDictionary *FindBase(uint32_t)const override{return &base;}
};
static void CompactLengthBoundaries() {
    NativeFixture fixture;
    const std::vector<StenoStroke> outline(17, StenoStroke(8));
    for (size_t limit = 1; limit <= 16; ++limit) {
        Bases bases(limit);
        NativeEngineBackend<NativeDevice> backend(fixture.store, bases);
        NativeOverlayDictionary dictionary(backend,2,"Upper");
        for (size_t length = 0; length <= 17; ++length) {
            auto result = dictionary.Lookup(StenoDictionaryLookup(outline.data(),length));
            assert(result.IsValid() == (length >= 1 && length <= limit));
            result.Destroy();
        }
        assert(bases.base.calls==limit);
    }
}
static void SearchVisibility() {
    NativeFixture fixture;
    constexpr uint32_t target = 2, key = 8;
    bool failed = false;
    auto visible = [&](bool enabled, bool owns) {
        LiveView view;
        assert(fixture.store.Snapshot(view));
        const bool result = NativeBaselineVisible(fixture.store.live, view, target, &key, 1, enabled,
                                                   [&] { return owns; }, [&] { failed = true; });
        fixture.store.Release(view);
        return result;
    };
    assert(visible(true, true));
    assert(!visible(false, true) && !visible(true, false));
    assert(fixture.Edit(target, key, "replacement") == LiveResult::Ok && !visible(true, true));
    assert(fixture.Edit(target, key, "", 1) == LiveResult::Ok && !visible(true, true));
    fixture.Open();
    assert(!visible(true, true));
    assert(fixture.Edit(target, key, "", 2) == LiveResult::Ok && visible(true, true));
    assert(!failed);
}
// Compare the engine's grouped journal probes with per-dictionary lookups
// over the same data. These are storage read counts, not measured board times.
static void GroupedReads() {
    constexpr uint32_t dictionaries = 16;
    NativeFixture fixture(dictionaries);
    Bases bases;
    NativeEngineBackend<NativeDevice> backend(fixture.store, bases);
    auto &live = fixture.store.live;
    static_assert(sizeof(OutlineEdits) <= 880);

    auto run = [&](const uint32_t *keys, uint8_t length, const char *phase) {
        LiveView pin;
        assert(fixture.store.Snapshot(pin));
        uint32_t separate_reads = 0, grouped_reads = 0;
        StenoStroke strokes[] = {StenoStroke(0), StenoStroke(0)};
        for (unsigned i = 0; i < length; ++i) strokes[i] = StenoStroke(keys[i]);
        const StenoDictionaryLookup lookup(strokes, length);
        for (uint32_t id = 1; id <= dictionaries; ++id) {
            RecordView expected;
            const auto before = fixture.nor.reads;
            const auto result = live.FindSnapshot(pin, id, keys, length, expected);
            separate_reads += fixture.nor.reads - before;
            assert(result == LiveResult::Ok || result == LiveResult::Missing);
            const auto state = result == LiveResult::Missing ? EditState::Missing :
                expected.operation == Operation::Set ? EditState::Set :
                expected.operation == Operation::Remove ? EditState::Remove : EditState::Reset;
            const auto start = fixture.nor.reads;
            uint32_t token;
            EditText got;
            assert(backend.Acquire(id, token));
            assert(backend.Find(token, id, lookup, got) == state);
            if (result == LiveResult::Ok)
                assert(got.address == expected.text_address && got.bytes == expected.text_bytes);
            backend.Release(token);
            grouped_reads += fixture.nor.reads - start;
        }
        fixture.store.Release(pin);
        assert(grouped_reads < separate_reads);
        std::cout << phase << ": " << separate_reads << " → " << grouped_reads << " flash reads\n";
    };
    // Every dictionary has plausible hits at the tested lengths. Only the
    // lowest priority dictionary has the target two-stroke outline.
    for (uint32_t id = 1; id <= dictionaries; ++id) {
        assert(fixture.Edit(id, id + 1, "one") == LiveResult::Ok);
        const uint32_t pair[] = {8, id + 1};
        assert(fixture.store.Edit(id, 0, pair, 2, reinterpret_cast<const uint8_t *>("two"), 3)
               == LiveResult::Ok);
    }
    const uint32_t missing[] = {1}, last[] = {8, dictionaries + 1};
    run(missing, 1, "tail miss");
    run(last, 2, "tail low-priority hit");
    assert(fixture.store.Start(NativeStore<NativeDevice>::Work::Checkpoint) == JobResult::More);
    fixture.Finish();
    fixture.Open();
    assert(!live.TailCount());
    run(missing, 1, "checkpoint miss");
    run(last, 2, "checkpoint low-priority hit");
    LiveView pin;
    OutlineEdits misses;
    RecordView record;
    assert(fixture.store.Snapshot(pin));
    assert(live.FindOutline(pin, misses, 1, missing, 1, record) == LiveResult::Missing);
    assert(live.FindOutline(pin, misses, dictionaries, last, 2, record) == LiveResult::Ok);
    const auto reads = fixture.nor.reads;
    assert(live.FindOutline(pin, misses, 1, missing, 1, record) == LiveResult::Missing);
    assert(fixture.nor.reads == reads);
    fixture.store.Release(pin);
    assert(fixture.Edit(1, missing[0], "now present") == LiveResult::Ok);
    assert(fixture.store.Snapshot(pin));
    assert(live.FindOutline(pin, misses, 1, missing, 1, record) == LiveResult::Ok);
    fixture.store.Release(pin);

    for (uint32_t id = 1; id <= dictionaries; ++id)
        assert(fixture.Edit(id, 8, "shared") == LiveResult::Ok);
    const uint32_t shared[] = {8};
    assert(fixture.Edit(3, 8, "", 1) == LiveResult::Ok);
    assert(fixture.Edit(5, 8, "", 2) == LiveResult::Ok);
    run(shared, 1, "shared outline with tombstone and reset");
    assert(fixture.Edit(3, 8, "revived") == LiveResult::Ok);
    run(shared, 1, "same outline after append");

    // Earlier catalog pins retain their old answers even after a newer edit.
    LiveView old, newer;
    OutlineEdits cache;
    assert(fixture.store.Snapshot(old));
    assert(live.FindOutline(old, cache, 3, shared, 1, record) == LiveResult::Ok);
    const auto old_address = record.text_address;
    for (unsigned i = 0; i < 70; ++i)
        assert(fixture.Edit(3, 8, "edit") == LiveResult::Ok);
    assert(fixture.Edit(3, 8, "", 1) == LiveResult::Ok);
    assert(fixture.store.Snapshot(newer));
    assert(live.FindOutline(newer, cache, 3, shared, 1, record) == LiveResult::Ok);
    assert(record.operation == Operation::Remove);
    assert(live.FindOutline(newer, cache, 5, shared, 1, record) == LiveResult::Ok);
    assert(record.operation == Operation::Reset);
    assert(live.FindOutline(old, cache, 3, shared, 1, record) == LiveResult::Ok);
    assert(record.operation == Operation::Set && record.text_address == old_address);
    fixture.store.Release(newer);
    fixture.store.Release(old);
}

int main() {
    CompactLengthBoundaries();
    SearchVisibility();
    GroupedReads();
    NativeFixture f;Bases bases;NativeEngineBackend<NativeDevice> backend(f.store,bases);
    NativeOverlayDictionary dictionary(backend,2,"Upper");const StenoStroke stroke(8);const StenoDictionaryLookup lookup(&stroke,1);
    auto base=dictionary.Lookup(lookup);assert(base.IsValid() && !std::strcmp(base.GetText(),"cat"));base.Destroy();
    const uint32_t edited_outline[] = {8, 16, 32};
    const uint8_t edit[] = {'l', 'o', 'n', 'g'};
    assert(f.store.Edit(2,0,edited_outline,3,edit,sizeof(edit))==LiveResult::Ok);
    const StenoStroke hit[] = {StenoStroke(8), StenoStroke(16), StenoStroke(32)};
    const StenoStroke miss[] = {StenoStroke(8), StenoStroke(16), StenoStroke(64)};
    auto edited=dictionary.Lookup(StenoDictionaryLookup(hit,3));
    assert(edited.IsValid() && !std::strcmp(edited.GetText(),"long"));edited.Destroy();
    assert(!dictionary.Lookup(StenoDictionaryLookup(miss,3)).IsValid() && bases.base.calls==1);
    for (uint32_t special : {0u, 1u << 25, 1u << 31}) {
        for (size_t position = 0; position < 3; ++position) {
            StenoStroke invalid[] = {hit[0], hit[1], hit[2]};
            invalid[position] = StenoStroke(special);
            assert(!dictionary.Lookup(StenoDictionaryLookup(invalid,3)).IsValid());
            assert(bases.ready && bases.base.calls==1);
        }
    }
    assert(f.Edit(2,8,"native")==LiveResult::Ok);auto old=dictionary.Lookup(lookup);
    assert(old.IsValid() && !std::strcmp(old.GetText(),"native"));
    assert(f.Edit(2,8,"newer")==LiveResult::Ok && !std::strcmp(old.GetText(),"native"));
    assert(f.store.Start(NativeStore<NativeDevice>::Work::Checkpoint)==JobResult::More);
    for(;;) {
        auto result=dictionary.Lookup(lookup);assert(result.IsValid() && !std::strcmp(result.GetText(),"newer"));result.Destroy();
        const auto step=f.store.Step(f.Check());if(step==JobResult::Done)break;assert(step==JobResult::More);
    }
    old.Destroy();f.Open();
    edited=dictionary.Lookup(StenoDictionaryLookup(hit,3));
    assert(edited.IsValid() && !std::strcmp(edited.GetText(),"long"));edited.Destroy();
    assert(!dictionary.Lookup(StenoDictionaryLookup(miss,3)).IsValid() && bases.base.calls==1);
    assert(f.store.Edit(2,2,edited_outline,3,nullptr,0)==LiveResult::Ok);
    assert(!dictionary.Lookup(StenoDictionaryLookup(hit,3)).IsValid() && bases.base.calls==1);
    uint32_t token=0,other=0;assert(backend.Acquire(2,token) && !backend.Acquire(2,other) && !other && bases.ready);backend.Release(token);
    f.nor.fail_read=true;const auto calls=bases.base.calls;
    auto failed=dictionary.Lookup(lookup);assert(!failed.IsValid() && !bases.ready && bases.base.calls==calls);failed.Destroy();
    f.nor.fail_read=false;
    failed=dictionary.Lookup(lookup);assert(!failed.IsValid() && bases.base.calls==calls);failed.Destroy();
    std::cout<<"Native engine backend: owned text, maintenance snapshots, bounded pin nesting and no base fallback after IO failure passed\n";
}
