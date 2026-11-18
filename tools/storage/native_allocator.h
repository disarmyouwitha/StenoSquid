// Candidate-B 16 MiB allocation planning. No flash IO or automatic reclamation.
#pragma once
#include <cstdint>

namespace candidate_b {
struct SegmentMap {
    uint32_t words[8]{};
    bool Contains(uint32_t segment) const {
        return segment >= 256 || (words[segment/32] & (1u << (segment%32)));
    }
    bool Add(uint32_t start, uint32_t count) {
        if (!count || start >= 256 || count > 256-start) return false;
        for (uint32_t i=start;i<start+count;++i) words[i/32] |= 1u << (i%32);
        return true;
    }
    void Union(const SegmentMap &other) {
        for (unsigned i=0;i<8;++i) words[i] |= other.words[i];
    }
};
static_assert(sizeof(SegmentMap)==32);

// Start with the union of current, BOTH retained roots (even quarantined roots),
// and all pinned reader extents. Add pending transaction extents to this private
// plan, never to the durable ownership state. Failure leaves the plan unchanged.
class AllocationPlan {
    SegmentMap occupied;
public:
    explicit AllocationPlan(const SegmentMap &protectedSegments) : occupied(protectedSegments) {
        occupied.Add(0,2); occupied.Add(255,1); // Catalogs + reserved tail.
    }
    const SegmentMap &Map() const { return occupied; }
    uint32_t Free() const {
        uint32_t free=0;
        for (uint32_t i=2;i<255;++i) free += !occupied.Contains(i);
        return free;
    }
    bool Reserve(uint32_t count,uint32_t &start) {
        const uint32_t free=Free();
        if (!count || count>253 || free<3 || count>free-3) return false;
        uint32_t run=0;
        for (uint32_t i=2;i<255;++i) {
            run=occupied.Contains(i)?0:run+1;
            if (run==count) {
                const uint32_t first=i+1-count;
                occupied.Add(first,count);
                start=first;
                return true;
            }
        }
        return false;
    }
};
static_assert(sizeof(AllocationPlan)==32);
} // namespace candidate_b
