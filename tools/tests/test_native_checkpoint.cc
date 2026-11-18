#include "native_checkpoint.h"
#include <cassert>
#include <vector>
#include <map>
#include <string>
#include <algorithm>
#include <iostream>
using namespace candidate_b;
struct Row {uint32_t dictionary,key;int op;std::string text;};
struct Ops {
    std::vector<Row> rows;
    std::vector<uint32_t> input[2],output[2];
    unsigned comparisons=0,reads=0,emits=0;
    bool fail=false;
    int CompareKey(uint32_t a,uint32_t b) {
        ++comparisons;if(fail || a>=rows.size() || b>=rows.size())return 2;
        auto x=std::make_pair(rows[a].dictionary,rows[a].key),y=std::make_pair(rows[b].dictionary,rows[b].key);
        return x==y?0:x<y?-1:1;
    }
    int CompareReverse(uint32_t a,uint32_t b) {
        ++comparisons;if(fail || a>=rows.size() || b>=rows.size())return 2;
        if(rows[a].text!=rows[b].text)return rows[a].text<rows[b].text?-1:1;
        return CompareKey(a,b);
    }
    int Operation(uint32_t address){return fail || address>=rows.size()?-1:rows[address].op;}
    bool ReadIndex(bool reverse,uint32_t i,uint32_t &address) {
        ++reads;if(fail || i>=input[reverse].size())return false;
        address=input[reverse][i];return true;
    }
    bool Emit(bool reverse,uint32_t address) {
        ++emits;if(fail)return false;output[reverse].push_back(address);return true;
    }
};
int main() {
    uint32_t rng=43;
    auto random=[&](){rng^=rng<<13;rng^=rng>>17;rng^=rng<<5;return rng;};
    uint64_t steps=0;
    for(unsigned trial=0;trial<181;++trial) {
        Ops ops;
        std::map<std::pair<uint32_t,uint32_t>,uint32_t> expected;
        unsigned old=trial==180?200000:trial%3==0?0:trial%3==1?10:1000;
        for(unsigned i=0;i<old;++i) {
            uint32_t id=uint32_t(ops.rows.size());
            ops.rows.push_back({i%4,i/4,int(i%7==0),"word "+std::to_string(i%32)});
            expected[{ops.rows[id].dictionary,ops.rows[id].key}]=id;
        }
        for(auto entry:expected)ops.input[0].push_back(entry.second);
        for(auto entry:expected)if(!ops.rows[entry.second].op)ops.input[1].push_back(entry.second);
        std::sort(ops.input[1].begin(),ops.input[1].end(),[&](uint32_t a,uint32_t b){return ops.CompareReverse(a,b)<0;});
        Tail tail;
        unsigned edits=trial==180?256:trial%3==0?0:trial%3==1?1:256;
        for(unsigned i=0;i<edits;++i) {
            uint32_t address=ops.rows.size();
            ops.rows.push_back({random()%4,random()%300,int(random()%3),"new "+std::to_string(random()%32)});
            const auto &row=ops.rows.back();
            if(row.op==2)expected.erase({row.dictionary,row.key});
            else expected[{row.dictionary,row.key}]=address;
            assert(tail.Append(address));
        }
        CheckpointBuilder<Ops> builder(ops);
        static_assert(sizeof(builder)<=2200);
        assert(builder.Begin(tail,ops.input[0].size(),ops.input[1].size())==BuildResult::More);
        assert(builder.Begin(tail,0,0)==BuildResult::Invalid);
        for(unsigned call=0;call<2000000;++call) {
            ops.comparisons=ops.reads=ops.emits=0;
            auto status=builder.Step();++steps;
            assert(ops.reads<=1 && ops.emits<=1 && ops.comparisons<=12);
            if(status==BuildResult::Done)break;
            assert(status==BuildResult::More && call<1999999);
        }
        std::vector<uint32_t> forward,reverse;
        for(auto entry:expected) {
            forward.push_back(entry.second);
            if(!ops.rows[entry.second].op)reverse.push_back(entry.second);
        }
        std::sort(reverse.begin(),reverse.end(),[&](uint32_t a,uint32_t b){return ops.CompareReverse(a,b)<0;});
        assert(ops.output[0]==forward && ops.output[1]==reverse);
        assert(builder.ForwardCount()==forward.size() && builder.ReverseCount()==reverse.size());
        assert(builder.Step()==BuildResult::Done);
        // A cancelled build never resumes or publishes a root.
        assert(builder.Begin(tail,0,0)==BuildResult::More);
        builder.Cancel();assert(builder.Step()==BuildResult::Invalid);
        if(tail.count) {
            assert(builder.Begin(tail,ops.input[0].size(),ops.input[1].size())==BuildResult::More);
            ops.fail=true;
            bool failed=false;
            for(unsigned i=0;i<100;++i) {
                auto status=builder.Step();
                if(status!=BuildResult::More){assert(status==BuildResult::Corrupt || status==BuildResult::IoError);failed=true;break;}
            }
            assert(failed);
        }
    }
    Ops ops;CheckpointBuilder<Ops> builder(ops);Tail tail;
    tail.count=257;assert(builder.Begin(tail,0,0)==BuildResult::Invalid);
    tail.count=0;assert(builder.Begin(tail,kMaxEntries+1,0)==BuildResult::Invalid);
    std::cout<<"Native checkpoint: 181 independent map-oracle cases (including 200000 indexed records); "<<steps
             <<" bounded merge steps; stale reverse/reset/tombstone/latest-ID semantics passed\n";
}
