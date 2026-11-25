#pragma once
#ifdef CONFIG_ZMK_STENO_SQUID_MEMORY_DIAGNOSTICS
/* USB console command `memory_report`: heap and stack high-water marks as one JSON object. */
void steno_squid_memory_report(void *, const char *);
#endif