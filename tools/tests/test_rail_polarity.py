#!/usr/bin/env python3
"""Exercise the production rail test with simulated active-low/high supplies."""
from pathlib import Path
import subprocess
import tempfile
from hostlib import compiler

ROOT = Path(__file__).resolve().parents[2]
CODE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#define GPIO_ACTIVE_LOW 1
#define GPIO_OUTPUT_INACTIVE 0
#define NRF_GPIO_PIN_NOPULL 0
#define RAIL_SENSE_PIN 7
static struct mock_port { const char *name; } port = {"gpio0"};
static struct { struct mock_port *port; int pin; int dt_flags; } lf_power = {&port,31,0};
static int lf_console, raw, physical_on, sensor_override, timing_calls;
static char report[4096];
static void squid_flash_diag_printf(int console, const char *fmt, ...) {
    (void)console; va_list args; va_start(args,fmt);
    size_t len=strlen(report); vsnprintf(report+len,sizeof(report)-len,fmt,args); va_end(args);
}
static int gpio_pin_configure_dt(void *spec,int mode) {
    (void)spec;(void)mode;raw=(lf_power.dt_flags & GPIO_ACTIVE_LOW)?1:0;return 0;
}
static void take_flash_down(void) { gpio_pin_configure_dt(&lf_power,0); }
static void nrf_gpio_cfg_input(int pin,int pull) {(void)pin;(void)pull;}
static int gpio_pin_set_raw(void *p,int pin,int v) {(void)p;(void)pin;raw=v;return 0;}
static int nrf_gpio_pin_read(int pin) {(void)pin;return sensor_override>=0?sensor_override:raw==physical_on;}
static void k_msleep(int ms) {(void)ms;}
static int32_t wait_for_sense(bool wanted,uint32_t timeout) {
    (void)timeout;timing_calls++;assert(nrf_gpio_pin_read(7)==wanted);return 42;
}
'''
MAIN = r'''
int main(void) {
    for (physical_on=0;physical_on<=1;physical_on++) {
        for (int configured=0;configured<=1;configured++) {
            lf_power.dt_flags=configured?0:GPIO_ACTIVE_LOW;
            sensor_override=-1;timing_calls=0;report[0]=0;rail_test();
            assert(timing_calls==2);
            assert(strstr(report,physical_on?"HIGH enables":"LOW enables"));
            assert(strstr(report,physical_on==configured?"polarity matches":"polarity MISMATCH"));
            assert(strstr(report,"RAIL rise:") && strstr(report,"RAIL fall:"));
            assert(raw==!configured);
        }
    }
    for (sensor_override=0;sensor_override<=1;sensor_override++) {
        timing_calls=0;report[0]=0;rail_test();assert(timing_calls==0);
        assert(strstr(report,sensor_override?"reads high at both":"no rail seen"));
    }
    return 0;
}
'''

def main():
    source = (ROOT / 'src/steno_squid/dictionary_flash_diag.c').read_text()
    body = source[source.index('static void rail_test(void)'):source.index('/* T:')]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / 'test.c').write_text(CODE + body + MAIN)
        subprocess.run([*compiler('c'), '-std=gnu11', '-Wall', '-Wextra', '-Werror', str(path / 'test.c'), '-o', str(path / 'test')], check=True)
        subprocess.run([str(path / 'test')], check=True)
    print('PASS: both rail polarities, configuration mismatches, rise/fall and absent/stuck sense')

if __name__ == '__main__':
    main()
