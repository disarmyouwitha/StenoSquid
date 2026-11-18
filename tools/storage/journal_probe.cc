// Freestanding resource probe; no hardware writes or firmware linkage.
#include "native_journal.h"
#include "native_allocator.h"
struct ProbeDevice {
    bool (*read)(uint32_t,uint8_t *,uint32_t);
    bool (*program)(uint32_t,const uint8_t *,uint32_t);
    bool (*erase)(uint32_t);
    bool Read(uint32_t at,uint8_t *out,uint32_t n) { return read(at,out,n); }
    bool Program(uint32_t at,const uint8_t *data,uint32_t n) { return program(at,data,n); }
    bool Erase(uint32_t segment) { return erase(segment); }
};
using ProbeJournal = candidate_b::NativeJournal<ProbeDevice>;
static_assert(sizeof(ProbeJournal)<=272);
static_assert(sizeof(candidate_b::JournalScan)<=24);
extern "C" candidate_b::JournalResult ProbeAppend(ProbeJournal &journal,uint32_t at,
        uint64_t sequence,uint32_t id,uint8_t op,const uint32_t *strokes,uint8_t count,
        const uint8_t *text,uint16_t length) {
    return journal.Append(at,sequence,id,op,strokes,count,text,length);
}
extern "C" candidate_b::JournalResult ProbeScan(ProbeJournal &journal,
        candidate_b::JournalScan &scan,candidate_b::JournalRecord &record) {
    return journal.Step(scan,record);
}
extern "C" candidate_b::JournalResult ProbeBegin(ProbeJournal &journal,uint32_t segment,
        uint64_t epoch,uint64_t previous,candidate_b::JournalScan &scan) {
    return journal.BeginScan(segment,epoch,previous,scan);
}
extern "C" candidate_b::JournalResult ProbeInitialize(ProbeJournal &journal,
        uint32_t segment,uint64_t epoch) {
    return journal.Initialize(segment,epoch);
}
extern "C" bool ProbeAllocate(candidate_b::AllocationPlan &plan,uint32_t count,uint32_t &start) {
    return plan.Reserve(count,start);
}
