// Native flash allocation planning. No flash IO or automatic reclamation.
#pragma once
#include <cstdint>

namespace candidate_b {
constexpr uint32_t kSmallPoolEnd = 255;
constexpr uint32_t kLargePoolEnd = 1023;
constexpr uint32_t kMaxSegments = kLargePoolEnd + 1;

template<class Device>
auto NativePoolEndImpl(const Device &device, int) -> decltype(device.PoolEnd()) { return device.PoolEnd(); }

template<class Device>
uint32_t NativePoolEndImpl(const Device &, long) { return kSmallPoolEnd; }

template<class Device>
uint32_t NativePoolEnd(const Device &device) { return NativePoolEndImpl(device, 0); }

struct SegmentMap {
    uint32_t words[kMaxSegments / 32]{};
    bool Contains(uint32_t segment) const {
        return segment >= kMaxSegments || (words[segment/32] & (1u << (segment%32)));
    }
    bool Add(uint32_t start, uint32_t count) {
        if (!count || start >= kMaxSegments || count > kMaxSegments-start) return false;
        for (uint32_t i=start;i<start+count;++i) words[i/32] |= 1u << (i%32);
        return true;
    }
    void Union(const SegmentMap &other) {
        for (unsigned i=0;i<kMaxSegments / 32;++i) words[i] |= other.words[i];
    }
};


// Start with the union of current, BOTH retained roots (even quarantined roots),
// and all pinned reader extents. Add pending transaction extents to this private
// plan, never to the durable ownership state. Failure leaves the plan unchanged.
class AllocationPlan {
    SegmentMap occupied;
    uint32_t end;
public:
    explicit AllocationPlan(const SegmentMap &protectedSegments, uint32_t pool_end = kSmallPoolEnd)
        : occupied(protectedSegments), end(pool_end) {
        occupied.Add(0,2); occupied.Add(end,1); // Catalogs + reserved tail.
    }
    const SegmentMap &Map() const { return occupied; }
    uint32_t Free() const {
        uint32_t free=0;
        for (uint32_t i=2;i<end;++i) free += !occupied.Contains(i);
        return free;
    }
    bool Reserve(uint32_t count,uint32_t &start) {
        const uint32_t free=Free();
        if (!count || count>end-2 || free<3 || count>free-3) return false;
        uint32_t run=0;
        for (uint32_t i=2;i<end;++i) {
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

} // namespace candidate_b
