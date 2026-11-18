#!/usr/bin/env python3
"""Exercise the actual diagnostic handler with bounded scratch/QSPI doubles."""

from pathlib import Path
import subprocess
import tempfile
from hostlib import compiler

ROOT = Path(__file__).resolve().parents[2]
STUBS = r"""
#include <cassert>
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cstdarg>
#include <cstdio>
#include <string>
#include <algorithm>
#define DT_NODELABEL(x) x
#define DT_REG_ADDR_BY_NAME(x,y) 0x12000000u
#define NRF_QSPI_ERASE_LEN_4KB 0
#define NRFX_SUCCESS 0
bool locked=true;
struct JavelinEngineLock { explicit operator bool()const{return locked;} };
std::string output;
struct Console { static void Printf(const char *fmt,...) {
    char text[1024]; va_list args; va_start(args,fmt);
    vsnprintf(text,sizeof(text),fmt,args); va_end(args); output+=text;
} };
uint32_t clock_ticks;
uint32_t k_cycle_get_32(){return clock_ticks+=64;}
uint32_t k_uptime_get_32(){return clock_ticks/64;}
uint32_t k_cyc_to_us_ceil32(uint32_t n){return (n+63)/64;}
uint8_t flash[4096];
unsigned writes=0,erases=0,fail_write=0;
void bounds(uint32_t at,uint32_t n) {assert(at>=0xfff000 && at<=0x1000000 && n<=0x1000000-at);}
int nrfx_qspi_read(void *out,uint32_t n,uint32_t at) {
    bounds(at,n); assert(!(at%4) && !(n%4) && !(reinterpret_cast<uintptr_t>(out)%4));
    std::memcpy(out,flash+(at-0xfff000),n); return 0;
}
int nrfx_qspi_write(const void *in,uint32_t n,uint32_t at) {
    bounds(at,n); assert(!(at%4) && !(n%4) && !(reinterpret_cast<uintptr_t>(in)%4));
    assert(at/256==(at+n-1)/256);
    if(++writes==fail_write) return -1;
    const auto *data=static_cast<const uint8_t *>(in);
    for(uint32_t i=0;i<n;++i) {
        auto &byte=flash[at-0xfff000+i]; assert((byte&data[i])==data[i]); byte&=data[i];
    }
    return 0;
}
int nrfx_qspi_erase(int,uint32_t at) {
    assert(at==0xfff000); ++erases; std::memset(flash,255,sizeof(flash));return 0;
}
int squid_qspi_wait_ready(uint32_t){return 0;}
bool squid_qspi_reactivate(){return true;}
void copy_xip_bytes(void *out,const void *source,size_t size) {
    uintptr_t at=reinterpret_cast<uintptr_t>(source)-0x12000000u;
    bounds(at,size); std::memcpy(out,flash+(at-0xfff000),size);
}
"""
HARNESS = r"""
int main() {
    std::memset(flash,255,sizeof(flash));
    steno_squid_storage_probe(nullptr,"dictionary_storage_probe");
    assert(output.find("ERR")!=std::string::npos && !writes && !erases);
    output.clear(); locked=false;
    steno_squid_storage_probe(nullptr,"dictionary_storage_probe run");
    assert(output.find("ERR")!=std::string::npos && !writes && !erases);
    output.clear(); locked=true; flash[4095]=0;
    steno_squid_storage_probe(nullptr,"dictionary_storage_probe run");
    assert(output.find("nothing changed")!=std::string::npos && !writes && !erases);
    flash[4095]=255; output.clear();
    steno_squid_storage_probe(nullptr,"dictionary_storage_probe run");
    assert(output.find("\"passed\":true")!=std::string::npos);
    assert(output.find("\"cleanup\":true")!=std::string::npos);
    assert(output.find("\"records\":5")!=std::string::npos && erases==2);
    for(auto byte:flash) assert(byte==255);
    output.clear(); fail_write=writes+3;
    steno_squid_storage_probe(nullptr,"dictionary_storage_probe run");
    assert(output.find("\"passed\":false")!=std::string::npos && erases==3);
    auto old_writes=writes;
    output.clear(); steno_squid_storage_probe(nullptr,"dictionary_storage_probe run");
    assert(output.find("nothing changed")!=std::string::npos && writes==old_writes && erases==3);
    puts("Scratch probe: actual handler, strict DMA/page bounds, packed commits, XIP lookup, cleanup and safe refusal passed");
}
"""


def main():
    source = (ROOT / "src/steno_squid/storage_probe.cc").read_text()
    # Compile the actual handler, replacing only hardware/framework includes.
    source = "\n".join(
        line for line in source.splitlines() if not line.startswith("#include")
    )
    headers = "\n".join(
        f'#include "{ROOT / "tools/storage" / name}"'
        for name in (
            "native_journal.h",
            "packed_index.h",
            "binary_record.h",
            "reverse_cursor.h",
        )
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "test.cc").write_text(STUBS + headers + "\n" + source + HARNESS)
        subprocess.run(
            [
                *compiler(),
                "-std=c++17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(path / "test.cc"),
                "-o",
                str(path / "test"),
            ],
            check=True,
        )
        subprocess.run([str(path / "test")], check=True)


if __name__ == "__main__":
    main()
