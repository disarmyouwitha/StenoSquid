#!/usr/bin/env python3
"""Compile the actual flash-acquire path with deterministic contention/wake doubles."""
from pathlib import Path
from hostlib import compiler
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[2]
CODE=r'''
#include <assert.h>
#include <stdbool.h>
#define IDLE_OFF 1
#define K_FOREVER -1
#define K_MSEC(n) (n)
#define K_SECONDS(n) ((n)*1000)
static int flash_lock, lf_restart_done, k_sys_work_q;
static bool lf_idle_off, lf_mapped_ready, key_thread;
static int lock_result, locks, unlocks, wake_requests, waits, timeout;
static int atomic_get(const bool *p){return *p;}
static void lf_note_use(void){}
static void lf_request_restart(void){wake_requests++;}
static int k_current_get(void){return key_thread?1:2;}
static int k_work_queue_thread_get(int *queue){(void)queue;return 1;}
static int k_mutex_lock(int *lock,int ms){(void)lock;locks++;timeout=ms;return lock_result;}
static void k_mutex_unlock(int *lock){(void)lock;unlocks++;}
static int k_condvar_wait(int *cv,int *lock,int ms){(void)cv;(void)lock;(void)ms;waits++;lf_idle_off=false;lf_mapped_ready=true;return 0;}
#include "flash_access.h"
int main(void){
 key_thread=true;lf_mapped_ready=true;lock_result=-1;
 assert(!dictionary_flash_acquire() && timeout==10 && locks==1 && !waits);
 lf_mapped_ready=false;lf_idle_off=true;lock_result=0;locks=0;
 assert(!dictionary_flash_acquire() && wake_requests==1 && !locks && !waits);
 key_thread=false;
 assert(dictionary_flash_acquire() && waits==1 && timeout==K_FOREVER);
 dictionary_flash_release();assert(unlocks==2);
 key_thread=true;locks=0;
 assert(dictionary_flash_acquire() && locks==1 && timeout==10);dictionary_flash_release();
}
'''

def main():
 with tempfile.TemporaryDirectory() as directory:
  root=Path(directory);source=root/'test.c';source.write_text(CODE,encoding='utf-8')
  subprocess.run([*compiler('c'),'-std=c11','-Wall','-Wextra','-Werror','-I',str(ROOT/'src/steno_squid'),str(source),'-o',str(root/'test')],check=True)
  subprocess.run([str(root/'test')],check=True)
 print('PASS: bounded key-thread lock, nonblocking key wake and signalled host wake')

if __name__=='__main__':main()
