// Actual Javelin bridge backed by the native store facade. The engine lock
// serializes this short lookup session; exports own independent snapshots.
#pragma once
#include "native_dictionary_adapter.h"
#include "../../tools/storage/native_store.h"
namespace candidate_b {
// A baseline result remains visible only if no local edit shadows its
// outline and the active dictionary stack still selects this dictionary.
template<class Live, class Owner, class Fault>
bool NativeBaselineVisible(Live &live, const LiveView &view, uint32_t id,
                           const uint32_t *strokes, uint8_t length, bool enabled,
                           Owner owns, Fault fault) {
    if (!enabled) return false;
    RecordView record;
    const auto state = live.FindSnapshot(view, id, strokes, length, record);
    if (state != LiveResult::Ok && state != LiveResult::Missing) {
        fault();
        return false;
    }
    return (state == LiveResult::Missing || record.operation == Operation::Reset) && owns();
}

class NativeBaseProvider {
public:
    virtual bool Ready()const=0;
    virtual void Fault()=0;
    virtual StenoDictionaryLookupResult Lookup(uint32_t,const StenoDictionaryLookup &)=0;
protected:
    ~NativeBaseProvider()=default;
};
template<class Device>
class NativeEngineBackend final : public DictionaryBackend {
    NativeStore<Device> &store;
    NativeBaseProvider &bases;
    LiveView view{};
public:
    NativeEngineBackend(NativeStore<Device> &value,NativeBaseProvider &provider):store(value),bases(provider){}
    bool Acquire(uint32_t,uint32_t &token)override {
        token=0;
        if(!bases.Ready() || view.token || !store.Snapshot(view))return false;
        token=view.token;return true;
    }
    void Release(uint32_t token)override{if(token && token==view.token)store.Release(view);}
    EditState Find(uint32_t token,uint32_t id,const StenoDictionaryLookup &lookup,EditText &text)override {
        if(!token || token!=view.token || !lookup.strokes || !lookup.length || lookup.length>16)return EditState::Error;
        uint32_t strokes[16];for(size_t i=0;i<lookup.length;++i)strokes[i]=lookup.strokes[i].GetKeyState();
        RecordView record;
        const auto result=store.live.FindSnapshot(view,id,strokes,lookup.length,record);
        if(result==LiveResult::Missing)return EditState::Missing;
        if(result!=LiveResult::Ok){bases.Fault();return EditState::Error;}
        text={record.text_address,record.text_bytes};
        return record.operation==Operation::Set?EditState::Set:record.operation==Operation::Remove?EditState::Remove:EditState::Reset;
    }
    bool Read(uint32_t token,const EditText &text,char *out)override {
        const RecordView record{Operation::Set,text.address,text.bytes};
        const bool ok=token && token==view.token &&
            store.live.ReadText(view,record,reinterpret_cast<uint8_t *>(out),text.bytes)==LiveResult::Ok;
        if(!ok)bases.Fault();
        return ok;
    }
    StenoDictionaryLookupResult Base(uint32_t token,uint32_t id,const StenoDictionaryLookup &lookup)override {
        return bases.Ready() && token && token==view.token?bases.Lookup(id,lookup):StenoDictionaryLookupResult::CreateInvalid();
    }
};
}
