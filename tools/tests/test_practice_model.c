#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../../src/steno_squid/practice_model.h"
#include "../../src/steno_squid/split_status.h"
#include "../../src/steno_squid/practice_menu.h"

static uint32_t seed = 1234567;
static uint32_t random_word(void) { seed=seed*1664525u+1013904223u;return seed; }
int main(void) {
    assert(steno_squid_menu_first(0,2,3,3)==0);
    assert(steno_squid_menu_first(0,3,4,3)==1);
    assert(steno_squid_menu_first(1,0,4,3)==0);
    assert(steno_squid_menu_first(0,6,7,3)==4);
    assert(steno_squid_menu_first(4,3,7,3)==3);
    assert(steno_squid_menu_first(3,0,7,3)==0);
    struct steno_squid_practice_model model;
    steno_squid_model_init(&model);
    assert(sizeof(model.record) < 3800);
    assert(sizeof(steno_squid_words)/sizeof(steno_squid_words[0])==300);
    unsigned visits[300]={0};
    for(unsigned run=0;run<1000;run++) {
        steno_squid_model_begin(&model,random_word);
        bool seen[300]={0};
        for(unsigned round=0;round<10;round++) {
            const unsigned word=model.run.deck[round];
            assert(word<300 && !seen[word]);seen[word]=true;visits[word]++;
        }
    }
    for(unsigned i=0;i<300;i++)assert(visits[i]>0);
    steno_squid_model_init(&model);
    for(unsigned i=0;i<300;i++) {
        model.record.words[i].completed=100;
        model.record.words[i].first_try=100;
        model.record.words[i].last_session=1;
    }
    model.record.session=1;
    const unsigned base=steno_squid_word_weight(&model.record,0);
    model.record.words[1].misses=100;
    model.record.words[1].first_try=0;
    model.record.words[2].mean_ds=300;
    model.record.words[3].completed=model.record.words[3].first_try=0;
    assert(steno_squid_word_weight(&model.record,1)>base);
    assert(steno_squid_word_weight(&model.record,2)>base);
    assert(steno_squid_word_weight(&model.record,3)>base);
    unsigned hard=0,easy=0;
    for(unsigned run=0;run<2000;run++) {
        // Hold the age factor constant while checking weighted sampling.
        model.record.session=1;
        steno_squid_model_begin(&model,random_word);
        for(unsigned i=0;i<10;i++){hard+=model.run.deck[i]==1;easy+=model.run.deck[i]==0;}
    }
    assert(hard>easy*2);
    steno_squid_model_init(&model);
    steno_squid_model_begin(&model,random_word);
    const unsigned first=model.run.deck[0];
    assert(steno_squid_model_answer(&model,0,false,1000));
    assert(steno_squid_model_answer(&model,0,false,1500));
    assert(model.record.xp==0 && model.record.words[first].misses==2);
    assert(steno_squid_model_answer(&model,0,true,2200));
    assert(model.record.xp==2 && model.run.first_try==0 && model.run.review==first);
    assert(!steno_squid_model_answer(&model,0,true,2300));
    assert(model.record.xp==2); // Duplicate feedback/chords cannot earn XP.
    for(unsigned round=1;round<10;round++)assert(steno_squid_model_answer(&model,round,true,1000));
    assert(steno_squid_model_finish(&model));
    assert(model.record.xp==112 && model.run.earned==112 && model.run.first_try==9);
    assert(!steno_squid_model_finish(&model) && model.record.rounds==1);
    assert(model.record.words[first].completed==1 && model.record.words[first].first_try==0);
    unsigned into,needed;
    assert(steno_squid_pop_level(0,&into,&needed)==1 && into==0 && needed==100);
    assert(steno_squid_pop_level(112,&into,&needed)==2 && into==12 && needed==200);
    assert(steno_squid_pop_level(300,&into,&needed)==3 && into==0 && needed==300);
    steno_squid_model_begin(&model,random_word);
    const unsigned paused=model.run.deck[0];
    model.record.words[paused].mean_ds=27;
    assert(steno_squid_model_answer(&model,0,true,300000));
    assert(model.record.words[paused].mean_ds==27 && model.run.timed_count==0);
    model.record.xp=STENO_SQUID_XP_MAX-1;
    model.record.words[model.run.deck[1]].completed=UINT16_MAX;
    model.record.words[model.run.deck[1]].first_try=UINT16_MAX;
    assert(steno_squid_model_answer(&model,1,true,900));
    assert(model.record.xp==STENO_SQUID_XP_MAX);
    assert(model.record.words[model.run.deck[1]].completed==UINT16_MAX);
    /* Both hand drills share the same rules, independent of word history. */
    steno_squid_model_init(&model);
    steno_squid_model_hands_begin(&model);
    assert(sizeof(model.record)==3624); // New 300-word record fits one NVS sector.
    const struct steno_squid_word_stats empty_words[300]={{0}};
    assert(!steno_squid_model_answer(&model,0,true,1000));
    for(unsigned round=0;round<30;round++) {
        const unsigned before=model.record.xp;
        assert(steno_squid_model_hands_answer(&model,round,true,1000));
        assert(model.record.xp>before);
        assert(!steno_squid_model_hands_answer(&model,round,true,1000));
    }
    assert(model.run.earned==50 && model.run.hand_streak==30);
    assert(steno_squid_model_finish(&model));
    assert(model.run.earned==70 && model.record.xp==70);
    assert(!steno_squid_model_finish(&model));
    assert(!memcmp(model.record.words,empty_words,sizeof(empty_words)));
    steno_squid_model_hands_begin(&model);
    const unsigned previous=model.record.xp;
    for(unsigned round=0;round<30;round++) {
        assert(!steno_squid_model_hands_answer(&model,round,false,100));
        assert(model.record.xp==previous+round);
        assert(steno_squid_model_hands_answer(&model,round,true,1000));
    }
    assert(steno_squid_model_finish(&model));
    assert(model.run.earned==50 && model.record.xp==previous+50);
    assert(!memcmp(model.record.words,empty_words,sizeof(empty_words)));
    steno_squid_model_multi_begin(&model,random_word);
    bool multi_seen[STENO_SQUID_MULTI_COUNT]={false};
    for(unsigned i=0;i<10;i++) {
        assert(model.run.deck[i]<STENO_SQUID_MULTI_COUNT && !multi_seen[model.run.deck[i]]);
        multi_seen[model.run.deck[i]]=true;
        if(i==0)assert(!steno_squid_model_hands_answer(&model,i,false,1000));
        assert(steno_squid_model_hands_answer(&model,i,true,2000));
    }
    assert(steno_squid_model_finish(&model) && model.run.earned==112 && model.run.first_try==9);
    assert(!memcmp(model.record.words,empty_words,sizeof(empty_words)));
    steno_squid_model_begin(&model,random_word);
    const unsigned speed_xp=model.record.xp;
    assert(steno_squid_model_answer(&model,0,true,1000));
    assert(steno_squid_model_timeout(&model));
    assert(model.record.xp==speed_xp+10 && model.run.earned==10);
    assert(!steno_squid_model_finish(&model) && !steno_squid_model_timeout(&model));
    steno_squid_model_build_begin(&model,random_word);
    bool build_seen[STENO_SQUID_BUILD_COUNT]={false};
    for(unsigned i=0;i<10;i++) {
        assert(model.run.deck[i]<STENO_SQUID_BUILD_COUNT && !build_seen[model.run.deck[i]]);
        build_seen[model.run.deck[i]]=true;
        assert(steno_squid_model_hands_answer(&model,i,true,3000));
    }
    assert(steno_squid_model_finish(&model) && model.run.earned==120);
    /* Trouble never invents history or resets the persisted record. */
    steno_squid_model_init(&model);
    assert(!steno_squid_model_trouble_begin(&model,random_word));
    assert(!model.run.active && model.record.session==0);
    assert(steno_squid_trouble_count(&model.record)==0);
    model.record.words[299].misses=1; // Aborted word attempt still needs review.
    assert(steno_squid_model_trouble_begin(&model,random_word));
    for(unsigned i=0;i<10;i++) {
        assert(model.run.deck[i]==299);
        assert(steno_squid_model_answer(&model,i,true,1000));
    }
    assert(steno_squid_model_finish(&model));
    assert(model.record.xp==120 && !steno_squid_trouble_count(&model.record));
    model.record.words[1].completed=10;model.record.words[1].first_try=9;
    model.record.words[1].mean_ds=40;
    assert(!steno_squid_word_trouble(&model.record.words[1])); // Exact thresholds pass.
    model.record.words[1].mean_ds=41;
    model.record.words[2].completed=10;model.record.words[2].first_try=8;
    for(unsigned run=0;run<100;run++) {
        assert(steno_squid_model_trouble_begin(&model,random_word));
        for(unsigned i=0;i<10;i++) {
            assert(model.run.deck[i]==1 || model.run.deck[i]==2);
            if(i)assert(model.run.deck[i]!=model.run.deck[i-1]);
        }
    }
    for(unsigned i=10;i<30;i++)model.record.words[i].misses=1;
    for(unsigned run=0;run<100;run++) {
        assert(steno_squid_model_trouble_begin(&model,random_word));
        bool seen[300]={false};
        for(unsigned i=0;i<10;i++) {
            const unsigned word=model.run.deck[i];
            assert(steno_squid_word_trouble(&model.record.words[word]));
            assert(!seen[word]);seen[word]=true;
        }
    }
    assert(sizeof(model.record)==3624);
    puts("Adaptive practice: unique decks, weighted sampling, XP, hand streak bonuses, levels, timing, Trouble targeting/recovery and saturation passed");
}
