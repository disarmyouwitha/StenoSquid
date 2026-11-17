#!/usr/bin/env python3
"""Exercise the actual CRC scrubber with a powered/unpowered fake flash."""
from hostlib import compiler
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[2]
STUB=r'''
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <string.h>
#include <assert.h>
#define CHUNK_BYTES 4
#define IMAGE_COUNT 2
#define MIN(a,b) ((a)<(b)?(a):(b))
struct base_image {uint32_t offset,bytes,crc;bool verified;};
static struct base_image lf_images[2]={{0,8,0,true},{8,8,0,true}};
static uint8_t nor[16],lf_readback[4];
#define XIP_BASE ((uintptr_t)nor)
static bool lf_mapped_ready=true,lf_checked=true,lf_trusted=true,lf_suspended;
static unsigned reads,clears,notifications;
static int64_t now;
static int64_t k_uptime_get(void){return now;}
static bool atomic_get(const bool *p){return *p;}
static void atomic_clear(bool *p){*p=false;}
static void atomic_set(bool *p,bool value){*p=value;}
void lf_scrub_invalidated(void);
static bool lf_clear_trust(void){clears++;lf_trusted=false;lf_scrub_invalidated();return false;}
static void lapwing_flash_ready_changed(void){notifications++;}
static uint32_t crc32_ieee_update(uint32_t crc,const uint8_t *bytes,size_t length){
 crc=~crc;while(length--){crc^=*bytes++;for(unsigned i=0;i<8;i++)crc=(crc>>1)^((crc&1)?0xedb88320u:0);}return ~crc;
}
static void copy_xip_bytes(void *out,const void *in,size_t length){reads++;memcpy(out,in,length);}
'''
CODE=r'''
#include "lapwing_flash_scrub.c"
int main(void){
 for(unsigned i=0;i<16;i++)nor[i]=(uint8_t)i;
 for(unsigned i=0;i<2;i++)lf_images[i].crc=crc32_ieee_update(0,nor+8*i,8);
 assert(lf_scrub_wait_ms()==60000);
 now=60000;lf_scrub_step();assert(reads==1);
 lf_mapped_ready=false;now+=8;lf_scrub_step();assert(reads==1 && lf_scrub_wait_ms()==-1);
 lf_mapped_ready=true;
 for(unsigned i=0;i<3;i++){now+=8;lf_scrub_step();}
 assert(reads==4 && !clears && lf_mapped_ready && lf_scrub_wait_ms()>60000);
 nor[12]^=1;now+=24*60*60*1000LL;
 for(unsigned i=0;i<4;i++){lf_scrub_step();now+=8;}
 assert(clears==1 && notifications==1 && !lf_trusted && !lf_mapped_ready && lf_checked);
 assert(lf_images[0].verified && !lf_images[1].verified);
 lf_scrub_step();assert(reads==8);
}
'''

def main():
 with tempfile.TemporaryDirectory() as folder:
  root=Path(folder)
  shutil.copy(ROOT/'src/steno_squid/lapwing_flash_scrub.c',root)
  (root/'lapwing_flash_internal.h').write_text(STUB,encoding='utf-8')
  (root/'xip_load.h').touch();(root/'zephyr/sys').mkdir(parents=True);(root/'zephyr/sys/crc.h').touch()
  (root/'test.c').write_text(CODE,encoding='utf-8')
  subprocess.run([*compiler('c'),'-std=c11','-Wall','-Wextra','-Werror','-I',str(root),str(root/'test.c'),'-o',str(root/'test')],check=True)
  subprocess.run([str(root/'test')],check=True)
 print('PASS: CRC scrub slices, idle pause, daily interval and failed-image trust invalidation')

if __name__=='__main__':main()
