#pragma once
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif
void steno_squid_words_begin(void);
void steno_squid_hands_begin(void);
void steno_squid_hands_answer(unsigned round, bool correct, uint32_t elapsed_ms);
unsigned steno_squid_words_prompt(unsigned round);
void steno_squid_words_answer(unsigned round, bool correct, uint32_t elapsed_ms);
void steno_squid_practice_finish(void);
void steno_squid_practice_checkpoint(void);
void steno_squid_practice_end(void);
uint64_t steno_squid_practice_progress(void);
#ifdef __cplusplus
}
#endif
