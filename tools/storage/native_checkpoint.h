// Incremental Candidate-B ordered-index merge. Caller owns pinned immutable input
// roots and unpublished, preallocated output extents. No catalog writes here.
#pragma once
#include "packed_index.h"

namespace candidate_b {
enum class BuildResult { More, Done, Corrupt, IoError, Invalid };

// Ops:
// CompareKey(a,b), CompareReverse(a,b) -> -1/0/1; 2 on invalid/IO.
// Operation(address) -> 0=set,1=remove,2=reset; -1 on invalid/IO.
// ReadIndex(reverse,position,address), Emit(reverse,address) -> bool.
// Equal keys in the input tail must retain sequence order, newest last. Both a
// chronological tail and the native stable key-ordered tail satisfy this rule.
template <class Ops>
class CheckpointBuilder {
    enum class Phase { Idle, SortForward, Dedup, ReverseCopy, SortReverse,
                       Forward, Reverse, Done, Failed };
    Ops &ops;
    uint32_t forward[256]{}, reverse[256]{};
    uint32_t size=0,live=0,reverse_size=0,i=0,j=0,holding=0;
    uint32_t old_counts[2]{},old_pos=0,new_pos=0,old_address=0;
    uint32_t forward_count=0,reverse_count=0;
    bool loaded=false,sorting=false;
    Phase phase=Phase::Idle;

    BuildResult Fail(BuildResult result) { phase=Phase::Failed;return result; }
    BuildResult Emit(bool rev,uint32_t address) {
        if(!ops.Emit(rev,address))return Fail(BuildResult::IoError);
        if(rev)++reverse_count;else ++forward_count;
        return BuildResult::More;
    }
    int Compare(uint32_t a,uint32_t b,bool rev) {
        return rev?ops.CompareReverse(a,b):ops.CompareKey(a,b);
    }
    // A single insertion-sort comparison/shift per call. IO and long key
    // comparison costs are bounded by the record limits, never overlay size.
    BuildResult Sort(bool rev) {
        uint32_t *array=rev?reverse:forward;
        uint32_t count=rev?reverse_size:size;
        if(i>=count) {
            i=j=0;sorting=false;
            phase=rev?Phase::Forward:Phase::Dedup;
            return BuildResult::More;
        }
        if(!sorting){holding=array[i];j=i;sorting=true;}
        if(j) {
            const int order=Compare(array[j-1],holding,rev);
            if(order < -1 || order > 1)return Fail(BuildResult::Corrupt);
            if(order>0){array[j]=array[j-1];--j;return BuildResult::More;}
        }
        array[j]=holding;++i;sorting=false;
        return BuildResult::More;
    }
public:
    explicit CheckpointBuilder(Ops &value):ops(value){}
    BuildResult Begin(const Tail &tail,uint32_t old_forward,uint32_t old_reverse) {
        if(phase!=Phase::Idle && phase!=Phase::Done && phase!=Phase::Failed)return BuildResult::Invalid;
        if(tail.count>256 || old_forward>kMaxEntries || old_reverse>kMaxEntries ||
           old_forward>kMaxEntries-tail.count || old_reverse>kMaxEntries-tail.count)return BuildResult::Invalid;
        size=tail.count;live=reverse_size=0;i=1;j=0;sorting=false;
        old_counts[0]=old_forward;old_counts[1]=old_reverse;
        old_pos=new_pos=forward_count=reverse_count=0;loaded=false;
        for(uint32_t n=0;n<size;++n)forward[n]=tail.addresses[n];
        phase=Phase::SortForward;return BuildResult::More;
    }
    uint32_t ForwardCount()const{return forward_count;}
    uint32_t ReverseCount()const{return reverse_count;}
    void Cancel(){phase=Phase::Failed;}

    BuildResult Step() {
        if(phase==Phase::Idle || phase==Phase::Failed)return BuildResult::Invalid;
        if(phase==Phase::Done)return BuildResult::Done;
        if(phase==Phase::SortForward)return Sort(false);
        if(phase==Phase::SortReverse)return Sort(true);
        if(phase==Phase::Dedup) {
            if(i==size){i=0;phase=Phase::ReverseCopy;return BuildResult::More;}
            if(i+1<size) {
                const int order=ops.CompareKey(forward[i],forward[i+1]);
                if(order < -1 || order > 0)return Fail(BuildResult::Corrupt);
                if(!order){++i;return BuildResult::More;}
            }
            forward[live++]=forward[i++];return BuildResult::More;
        }
        if(phase==Phase::ReverseCopy) {
            if(i==live){i=1;phase=Phase::SortReverse;return BuildResult::More;}
            const int op=ops.Operation(forward[i]);
            if(op<0 || op>2)return Fail(BuildResult::Corrupt);
            if(!op)reverse[reverse_size++]=forward[i];
            ++i;return BuildResult::More;
        }
        const bool rev=phase==Phase::Reverse;
        const uint32_t *tail=rev?reverse:forward;
        const uint32_t count=rev?reverse_size:live;
        if(!loaded && old_pos<old_counts[rev]) {
            if(!ops.ReadIndex(rev,old_pos,old_address))return Fail(BuildResult::IoError);
            loaded=true;return BuildResult::More;
        }
        if(rev && loaded) {
            // A recent edit of ANY operation supersedes this checkpoint record.
            uint32_t low=0,high=live;
            while(low<high) {
                uint32_t mid=low+(high-low)/2;
                int order=ops.CompareKey(forward[mid],old_address);
                if(order < -1 || order > 1)return Fail(BuildResult::Corrupt);
                if(!order){loaded=false;++old_pos;return BuildResult::More;}
                if(order<0)low=mid+1;else high=mid;
            }
        }
        if(!loaded && new_pos==count) {
            if(rev){phase=Phase::Done;return BuildResult::Done;}
            phase=Phase::Reverse;old_pos=new_pos=0;return BuildResult::More;
        }
        int order=loaded?(new_pos<count?Compare(old_address,tail[new_pos],rev):-1):1;
        if(order < -1 || order > 1 || (rev && order==0))return Fail(BuildResult::Corrupt);
        if(order<0){loaded=false;++old_pos;return Emit(rev,old_address);}
        if(order==0){loaded=false;++old_pos;}
        uint32_t address=tail[new_pos++];
        int op=ops.Operation(address);
        if(op<0 || op>2 || (rev && op))return Fail(BuildResult::Corrupt);
        if(op==2)return BuildResult::More; // Reset hides old override; no new row.
        return Emit(rev,address);
    }
};
} // namespace candidate_b
