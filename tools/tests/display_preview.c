/* Native LVGL render regression using the real screen, drill and squid widgets. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../../src/steno_squid/display/screen.c"
#include "../../src/steno_squid/display/practice_widget.c"
static uint8_t pixels[68*160];
static uint8_t saved[68*160];
#include "../../src/steno_squid/display/pet_widget.c"
void steno_squid_portrait_init(void) {}
static void flush(lv_display_t *d, const lv_area_t *a, uint8_t *p) {
    memcpy(saved,p,sizeof(saved));
    lv_display_flush_ready(d);
}
static void save(const char *name) {
    lv_refr_now(NULL);
    char path[80]; snprintf(path,sizeof(path),"%s.pgm",name);
    FILE *out=fopen(path,"wb");assert(out);
    fprintf(out,"P5\n68 160\n255\n");
    for(unsigned i=0;i<sizeof(saved);i++) saved[i]=saved[i]>=128?255:0;
    fwrite(saved,1,sizeof(saved),out);fclose(out);
}
/* Pupils may float inside the mask. Every lower tentacle must join the mantle. */
static void check_tentacles_connected(void) {
    bool seen[48*48] = {0};
    unsigned queue[48*48], read = 0, count = 0;
    for (unsigned i = 0; i < 48*48; i++) {
        if (idle_pixels[i]) { queue[count++] = i; seen[i] = true; break; }
    }
    while (read < count) {
        const unsigned i = queue[read++], x = i % 48, y = i / 48;
        const int neighbors[] = {x ? (int)i-1 : -1, x < 47 ? (int)i+1 : -1,
                                 y ? (int)i-48 : -1, y < 47 ? (int)i+48 : -1};
        for (unsigned n = 0; n < 4; n++) {
            const int j = neighbors[n];
            if (j >= 0 && idle_pixels[j] && !seen[j]) {
                queue[count++] = j; seen[j] = true;
            }
        }
    }
    for (unsigned i = 34*48; i < 48*48; i++) assert(!idle_pixels[i] || seen[i]);
}

int main(void) {
    check_tentacles_connected();
    // Closing the eyes must also fill the old upper eye-mask patches.
    for (unsigned y=14; y<16; y++) {
        unsigned left=48, right=0;
        for(unsigned x=0;x<48;x++) if(idle_pixels[y*48+x]) {
            if(left==48) left=x; right=x;
        }
        for(unsigned x=left;x<=right && x<48;x++) assert(sleep_pixels[y*48+x]);
    }
    lv_init();lv_display_t *d=lv_display_create(68,160);
    lv_display_set_color_format(d,LV_COLOR_FORMAT_L8);
    lv_display_set_buffers(d,pixels,NULL,sizeof(pixels),LV_DISPLAY_RENDER_MODE_FULL);
    lv_display_set_flush_cb(d,flush);
    lv_screen_load(steno_squid_screen_create());
    steno_squid_screen_update(STENO_SQUID_MODE_SYMBOLS,NULL,0,85,false);
    assert(!strcmp(lv_label_get_text(mode_below),"Numpad"));
    save("numpad");
    assert(lv_obj_get_height(mode_below)<=20);
    steno_squid_screen_update(STENO_SQUID_MODE_TYPING,NULL,0,85,false); save("qwerty");
    assert(state == PET_LAYER_CHANGE);
    advance_frame(frame_timer);
    assert(frame_index == 1);
    steno_squid_pet_set_state(PET_TYPING);
    assert(state == PET_LAYER_CHANGE && frame_index == 1);
    steno_squid_screen_update(STENO_SQUID_MODE_TYPING,NULL,0,100,false);
    assert(frame_index == 1); // A battery update must not restart the reaction.
    advance_frame(frame_timer);advance_frame(frame_timer);
    assert(state == PET_IDLE && lv_timer_get_paused(frame_timer));
    save("pop-idle");
    steno_squid_screen_update(STENO_SQUID_MODE_UNKNOWN,NULL,0,85,false); save("waiting");
    assert(lv_obj_get_y(mode_above)==17);
    assert(!strcmp(lv_label_get_text(mode_above),"Waiting"));
    assert(lv_obj_get_y(mode_below)==99);
    assert(!strcmp(lv_label_get_text(mode_below),"For Left"));
    steno_squid_screen_update(STENO_SQUID_MODE_TYPING,NULL,0,85,false);
    assert(state != PET_LAYER_CHANGE); // Reconnection keeps the greeting path.
    steno_squid_screen_update(STENO_SQUID_MODE_TYPING,NULL,0,85,true);
    save("host-disconnected");
    assert(!lv_obj_has_flag(mode_above, LV_OBJ_FLAG_HIDDEN));
    assert(!strcmp(lv_label_get_text(mode_above), "Discon-"));
    assert(!strcmp(lv_label_get_text(mode_below), "-nected"));
    assert(lv_obj_get_height(mode_above) ==
           lv_font_get_line_height(lv_obj_get_style_text_font(mode_above, LV_PART_MAIN)));
    steno_squid_screen_update(STENO_SQUID_MODE_UNKNOWN,NULL,0,85,true);
    save("waiting-without-host");
    assert(!strcmp(lv_label_get_text(mode_above), "Waiting"));
    steno_squid_screen_update(STENO_SQUID_MODE_TYPING,NULL,0,85,false);
    assert(lv_obj_has_flag(mode_above, LV_OBJ_FLAG_HIDDEN));
    assert(!strcmp(lv_label_get_text(mode_below), "Qwerty"));
    lv_obj_update_layout(mode_below);
    assert(lv_obj_get_y(mode_below)==99);
    steno_squid_screen_update(STENO_SQUID_MODE_COUNT,"Test",0,85,false);
    assert(state == PET_LAYER_CHANGE);
    advance_frame(frame_timer);
    steno_squid_screen_update(STENO_SQUID_MODE_COUNT,"Test",0,85,false);
    assert(frame_index == 1);
    steno_squid_screen_update(STENO_SQUID_MODE_COUNT,"Other",0,85,false);
    assert(frame_index == 0); // Custom layer names can also select different layers.
    steno_squid_screen_update(STENO_SQUID_MODE_PLOVER,NULL,0,85,false); save("plover");
    for(unsigned i=0;i<STENO_SQUID_PRACTICE_DRILL_COUNT;i++) {
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,
           STENO_SQUID_PRACTICE_ACTIVE|STENO_SQUID_PRACTICE_MENU|(i<<STENO_SQUID_PRACTICE_DRILL_SHIFT),85,false);
        char name[30];snprintf(name,sizeof(name),"welcome-%u",i);save(name);
        for(unsigned j=0;j<3;j++) assert(lv_obj_get_height(drill_choices[j])<=22);
    }
    steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,STENO_SQUID_PRACTICE_ACTIVE,85,false);
    save("cat");
    /* The stroke tape: shown on the Javelin layer only, and always on one line each. */
    {
        struct steno_squid_tape tape;
        memset(&tape,0,sizeof(tape));
        tape.valid=true; tape.wpm=87; tape.mask=0x8u|0x100u|0x80000u;
        snprintf(tape.text,sizeof(tape.text),"%s","cat");
        steno_squid_screen_set_tape(&tape);
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,0,85,false);
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_TRANSLATION
        assert(!lv_obj_has_flag(tape_text,LV_OBJ_FLAG_HIDDEN));
        assert(!lv_obj_has_flag(tape_stroke,LV_OBJ_FLAG_HIDDEN));
        assert(!lv_obj_has_flag(tape_wpm,LV_OBJ_FLAG_HIDDEN));
        assert(lv_obj_has_flag(tape_hint,LV_OBJ_FLAG_HIDDEN));
        assert(!strcmp(lv_label_get_text(tape_stroke),"KAT"));
        assert(!strcmp(lv_label_get_text(tape_wpm),"87 WPM"));
        assert(!headroom);
        lv_refr_now(NULL);
        assert(lv_obj_get_y(tape_text) + lv_obj_get_height(tape_text) <= lv_obj_get_y(tape_stroke));
        assert(lv_obj_get_y(tape_stroke) + lv_obj_get_height(tape_stroke) <= 48);
        save("tape-word");
#else
        assert(lv_obj_has_flag(tape_text,LV_OBJ_FLAG_HIDDEN));
        assert(lv_obj_has_flag(tape_stroke,LV_OBJ_FLAG_HIDDEN));
        assert(lv_obj_has_flag(tape_wpm,LV_OBJ_FLAG_HIDDEN));
        assert(headroom); // Both translation and hints off: animations get the space.
        save("tape-disabled");
#endif
        /* The widest text each row can be given. */
        snprintf(tape.text,sizeof(tape.text),"%s","WWWWWWWWWWWW");
        tape.mask=STENO_SQUID_TAPE_KEYS_MASK;
        tape.hint_count=2; tape.hint_mask=tape.mask;
        steno_squid_screen_set_tape(&tape);
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,0,85,false);
        assert(!lv_obj_has_flag(tape_hint,LV_OBJ_FLAG_HIDDEN));
        assert(lv_obj_has_flag(tape_wpm,LV_OBJ_FLAG_HIDDEN));
        lv_refr_now(NULL);
        assert(!headroom); // Hints alone also reserve the upper area.
        assert(lv_obj_get_height(tape_text)<=24);
        assert(lv_obj_get_height(tape_stroke)<=20);
        assert(lv_obj_get_height(tape_hint)<=12);
        assert(lv_text_get_width(lv_label_get_text(tape_text),strlen(lv_label_get_text(tape_text)),
                                 lv_obj_get_style_text_font(tape_text,0),0)<=TAPE_WIDTH);
        assert(lv_obj_get_y(tape_text) + lv_obj_get_height(tape_text) <= lv_obj_get_y(tape_stroke));
        assert(lv_obj_get_y(tape_stroke) + lv_obj_get_height(tape_stroke) <= 48);
        assert(lv_obj_get_y(tape_hint) + lv_obj_get_height(tape_hint) <= 140);
        save("tape-widest");
        /* A real two-stroke word with a one-stroke shortcut. */
        snprintf(tape.text,sizeof(tape.text),"%s","hello");
        tape.mask=0x40u|0x80u|0x200u|0x800u; /* HROE */
        tape.hint_count=1; tape.hint_mask=0x40u|0x20000u;
        steno_squid_screen_set_tape(&tape);
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,0,85,false);
        save("tape-shortcut");
#ifndef CONFIG_ZMK_STENO_SQUID_STROKE_TRANSLATION
        tape.hint_count=0;
        steno_squid_screen_set_tape(&tape);
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,0,85,false);
        assert(headroom); // Clearing the hint restores animations immediately.
#endif
        /* Other layers and Practice hide the tape. */
        steno_squid_screen_update(STENO_SQUID_MODE_TYPING,NULL,0,85,false);
        assert(lv_obj_has_flag(tape_text,LV_OBJ_FLAG_HIDDEN));
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,STENO_SQUID_PRACTICE_ACTIVE,85,false);
        assert(lv_obj_has_flag(tape_stroke,LV_OBJ_FLAG_HIDDEN));
        memset(&tape,0,sizeof(tape));
        steno_squid_screen_set_tape(&tape);
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,0,85,false);
        assert(lv_obj_has_flag(tape_text,LV_OBJ_FLAG_HIDDEN));
    }
    /* Every word ID must render on one line without covering the hint. */
    for (unsigned word=0;word<STENO_SQUID_WORD_COUNT;word++) {
        const uint32_t snapshot=STENO_SQUID_PRACTICE_ACTIVE|steno_squid_practice_pack_prompt(word);
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,snapshot,85,false);
        lv_refr_now(NULL);
        assert(!strcmp(lv_label_get_text(prompt_label),steno_squid_words[word].word));
        assert(lv_obj_get_y(prompt_label)+lv_obj_get_height(prompt_label)<=lv_obj_get_y(outline_label));
        assert(lv_obj_get_y(outline_label)+lv_obj_get_height(outline_label)<=lv_obj_get_y(feedback_label));
        if(!strcmp(steno_squid_words[word].word,"PEOPLE"))save("adaptive-word");
    }
    {
        const uint64_t summary=232 | (uint64_t)8<<20 | (uint64_t)3<<24 | (uint64_t)27<<30 |
                               (uint64_t)104<<39 | (uint64_t)99<<46;
        steno_squid_practice_widget_set_progress(summary);
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,
            STENO_SQUID_PRACTICE_ACTIVE|STENO_SQUID_PRACTICE_MENU,85,false);
        assert(!strcmp(lv_label_get_text(navigation_label)," *-/-*v"));
        save("adaptive-menu");
        const uint32_t trouble=STENO_SQUID_PRACTICE_ACTIVE|
            (STENO_SQUID_PRACTICE_TROUBLE<<STENO_SQUID_PRACTICE_DRILL_SHIFT);
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,trouble|STENO_SQUID_PRACTICE_MENU,85,false);
        lv_refr_now(NULL);
        assert(menu_first==1);
        assert(!strcmp(lv_label_get_text(drill_choices[2]),"TROUBLE"));
        assert(!strcmp(lv_label_get_text(start_label),"A/O: GO"));
        save("trouble-menu");
        steno_squid_practice_widget_set_progress(summary|((uint64_t)1<<53));
        assert(!strcmp(lv_label_get_text(start_label),"Try Words"));
        save("trouble-empty");
        steno_squid_practice_widget_set_progress(summary);
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,trouble|steno_squid_practice_pack_prompt(99),85,false);
        assert(!strcmp(lv_label_get_text(drill_label),"TROUBLE"));
        assert(!strcmp(lv_label_get_text(prompt_label),"GOOD"));
        save("trouble-word");
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,STENO_SQUID_PRACTICE_ACTIVE|
            (STENO_SQUID_PRACTICE_DONE<<STENO_SQUID_PRACTICE_FEEDBACK_SHIFT)|
            (10<<STENO_SQUID_PRACTICE_ROUND_SHIFT),85,false);
        assert(!strcmp(lv_label_get_text(prompt_label),"8/10"));
        assert(!strcmp(lv_label_get_text(progress_label),"Lv 2"));
        assert(lv_obj_get_style_text_decor(drill_label,0)==LV_TEXT_DECOR_UNDERLINE);
        assert(!lv_timer_get_paused(results_timer));
        lv_refr_now(NULL);
        assert(lv_obj_get_y(prompt_label)+lv_obj_get_height(prompt_label)+10<=48);
        save("adaptive-results-xp");
        lv_tick_inc(2499);lv_timer_handler();assert(result_card==0);
        lv_tick_inc(1);lv_timer_handler();assert(result_card==1);
        save("adaptive-results-time");
        for(unsigned card=1;card<3;card++) {
            result_tick(results_timer);
            save(card==1 ? "adaptive-results-review" : "adaptive-results-level");
        }
        const uint64_t maximum=0xfffff | (uint64_t)10<<20 | (uint64_t)63<<24 |
                               (uint64_t)511<<30 | (uint64_t)120<<39 | (uint64_t)99<<46;
        steno_squid_practice_widget_set_progress(maximum);
        for(unsigned card=0;card<4;card++) {
            lv_refr_now(NULL);
            assert(lv_obj_get_y(best_label)+lv_obj_get_height(best_label)<=130);
            assert(lv_obj_get_y(progress_label)+lv_obj_get_height(progress_label)<=lv_obj_get_y(best_label));
            assert(lv_text_get_width(lv_label_get_text(best_label),strlen(lv_label_get_text(best_label)),
                                    lv_obj_get_style_text_font(best_label,0),0)<=66);
            result_tick(results_timer);
        }
        steno_squid_screen_update(STENO_SQUID_MODE_TYPING,NULL,0,85,false);
        assert(lv_timer_get_paused(results_timer));
        const uint64_t hands=342 | (uint64_t)15<<30 | (uint64_t)110<<39 | (uint64_t)127<<46;
        steno_squid_practice_widget_set_progress(hands);
        steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,STENO_SQUID_PRACTICE_ACTIVE|
            (STENO_SQUID_PRACTICE_LEFT<<STENO_SQUID_PRACTICE_DRILL_SHIFT)|
            (STENO_SQUID_PRACTICE_DONE<<STENO_SQUID_PRACTICE_FEEDBACK_SHIFT)|
            (50<<STENO_SQUID_PRACTICE_ROUND_SHIFT)|(50<<STENO_SQUID_PRACTICE_BEST_SHIFT),85,false);
        assert(!strcmp(lv_label_get_text(prompt_label),"50/50"));
        assert(!strcmp(lv_label_get_text(best_label),"+110 XP"));
        save("adaptive-hands-xp");
        result_tick(results_timer);
        assert(!strcmp(lv_label_get_text(best_label),"Best 50"));
        save("adaptive-hands-streak");
        /* A finished celebration must not pick the idle keyboard's sleep pose
         * or USB sparkles while its score/menu text is still on screen. */
        preview_activity=ZMK_ACTIVITY_IDLE;
        steno_squid_pet_set_powered(false);
        assert(awake_hold);
        for(unsigned i=0;i<20 && state==PET_LEVEL_UP;i++)advance_frame(frame_timer);
        assert(state==PET_IDLE);
        assert(lv_obj_has_flag(effect_image,LV_OBJ_FLAG_HIDDEN));
        steno_squid_pet_set_state(PET_SLEEP);
        assert(state==PET_IDLE);
        steno_squid_pet_set_powered(true);
        assert(state==PET_IDLE);
        save("adaptive-win-awake");
        steno_squid_pet_set_powered(false);
        steno_squid_screen_update(STENO_SQUID_MODE_TYPING,NULL,0,85,false);
        assert(!awake_hold);
        steno_squid_pet_set_state(PET_SLEEP);
        assert(state==PET_SLEEP && !lv_obj_has_flag(effect_image,LV_OBJ_FLAG_HIDDEN));
        preview_activity=ZMK_ACTIVITY_ACTIVE;
        steno_squid_pet_set_state(PET_IDLE);
    }
    for (unsigned pass=0; pass<30; pass++) {
        for (unsigned mode=0; mode<STENO_SQUID_MODE_COUNT; mode++) {
            steno_squid_screen_update(mode,NULL,0,pass+65,false);
            lv_refr_now(NULL);
            assert(!lv_obj_has_flag(pet_image,LV_OBJ_FLAG_HIDDEN));
        }
        steno_squid_pet_set_powered(pass%2);
        steno_squid_pet_set_state(PET_TYPING);
        for(unsigned tick=0; tick<12; tick++) {
            lv_tick_inc(100); lv_timer_handler(); lv_refr_now(NULL);
        }
        for(unsigned drill=0; drill<STENO_SQUID_PRACTICE_DRILL_COUNT; drill++) {
            const uint32_t base=STENO_SQUID_PRACTICE_ACTIVE|(drill<<STENO_SQUID_PRACTICE_DRILL_SHIFT);
            steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,base|STENO_SQUID_PRACTICE_MENU,85,false);
            lv_refr_now(NULL);
            assert(lv_obj_has_flag(pet_image,LV_OBJ_FLAG_HIDDEN));
            steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,base,85,false); lv_refr_now(NULL);
            for(unsigned feedback=1;feedback<3;feedback++) {
                steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,
                   base|(feedback<<STENO_SQUID_PRACTICE_FEEDBACK_SHIFT),85,false);lv_refr_now(NULL);
            }
            const unsigned total=steno_squid_practice_rounds(drill);
            steno_squid_screen_update(STENO_SQUID_MODE_JAVELIN,NULL,base|
                 (STENO_SQUID_PRACTICE_DONE<<STENO_SQUID_PRACTICE_FEEDBACK_SHIFT)|
                 (total<<STENO_SQUID_PRACTICE_ROUND_SHIFT),85,false);
            lv_refr_now(NULL);
            assert(!lv_obj_has_flag(pet_image,LV_OBJ_FLAG_HIDDEN));
        }
    }
    lv_mem_monitor_t mon;lv_mem_monitor(&mon);
    printf("Heap total %zu free %zu max used %zu\n",mon.total_size,mon.free_size,mon.max_used);
    assert(mon.free_size >= 4096);
    printf("Configured heap: %u\n",(unsigned)LV_MEM_SIZE);
    puts("Status, squid, Practice menu and repeated transition checks passed");
}
