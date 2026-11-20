#include "native_engine_backend.h"
#include "native_store_fixture.h"
#include <iostream>
struct Bases : candidate_b::NativeBaseProvider {
    bool ready=true;unsigned calls=0;
    bool Ready()const override{return ready;}
    void Fault()override{ready=false;}
    StenoDictionaryLookupResult Lookup(uint32_t,const StenoDictionaryLookup &)override {
        ++calls;char *text=static_cast<char *>(std::malloc(4));std::memcpy(text,"cat",4);
        return StenoDictionaryLookupResult::CreateDynamicString(text);
    }
};
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
int main() {
    SearchVisibility();
    NativeFixture f;Bases bases;NativeEngineBackend<NativeDevice> backend(f.store,bases);
    NativeOverlayDictionary dictionary(backend,2,"Upper");const StenoStroke stroke(8);const StenoDictionaryLookup lookup(&stroke,1);
    auto base=dictionary.Lookup(lookup);assert(base.IsValid() && !std::strcmp(base.GetText(),"cat"));base.Destroy();
    assert(f.Edit(2,8,"native")==LiveResult::Ok);auto old=dictionary.Lookup(lookup);
    assert(old.IsValid() && !std::strcmp(old.GetText(),"native"));
    assert(f.Edit(2,8,"newer")==LiveResult::Ok && !std::strcmp(old.GetText(),"native"));
    assert(f.store.Start(NativeStore<NativeDevice>::Work::Checkpoint)==JobResult::More);
    for(;;) {
        auto result=dictionary.Lookup(lookup);assert(result.IsValid() && !std::strcmp(result.GetText(),"newer"));result.Destroy();
        const auto step=f.store.Step(f.Check());if(step==JobResult::Done)break;assert(step==JobResult::More);
    }
    old.Destroy();f.Open();
    uint32_t token=0,other=0;assert(backend.Acquire(2,token) && !backend.Acquire(2,other) && !other && bases.ready);backend.Release(token);
    f.nor.fail_read=true;const auto calls=bases.calls;
    auto failed=dictionary.Lookup(lookup);assert(!failed.IsValid() && !bases.ready && bases.calls==calls);failed.Destroy();
    f.nor.fail_read=false;
    failed=dictionary.Lookup(lookup);assert(!failed.IsValid() && bases.calls==calls);failed.Destroy();
    std::cout<<"Native engine backend: owned text, maintenance snapshots, bounded pin nesting and no base fallback after IO failure passed\n";
}
