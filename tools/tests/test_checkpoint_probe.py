#!/usr/bin/env python3
"""Exercise the actual native catalog/index diagnostic with strict QSPI doubles."""

from pathlib import Path
import subprocess
import tempfile
from hostlib import compiler
from test_storage_probe import STUBS

ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK = r"""
#define SYS_REBOOT_COLD 0
bool rebooted=false;
struct k_work {};
struct k_work_delayable { void (*function)(k_work *); };
#define K_WORK_DELAYABLE_DEFINE(name,function) k_work_delayable name{function}
#define K_SECONDS(x) (x)
void sys_reboot(int){rebooted=true;}
int k_work_reschedule(k_work_delayable *work,int){work->function(nullptr);return 0;}
"""
HARNESS = r"""
void command(const char *suffix) {
    output.clear();std::string line="dictionary_checkpoint_probe ";line+=suffix;
    steno_squid_checkpoint_probe(nullptr,line.c_str());
}
int main() {
    std::memset(flash,255,sizeof(flash));
    command("bad");assert(output.find("ERR")!=std::string::npos && !writes && !erases);
    locked=false;command("hold");assert(output.find("ERR")!=std::string::npos && !writes && !erases);
    locked=true;flash[20000]=0;command("hold");
    assert(output.find("nothing changed")!=std::string::npos && !writes && !erases);
    flash[20000]=255;command("hold");
    assert(output.find("\"passed\":true")!=std::string::npos);
    assert(output.find("\"generation\":2")!=std::string::npos);
    command("hold");assert(output.find("nothing changed")!=std::string::npos);
    command("live");assert(output.find("\"passed\":true")!=std::string::npos);
    command("live");assert(output.find("\"passed\":false")!=std::string::npos);
    command("job");assert(output.find("\"passed\":true")!=std::string::npos);
    assert(output.find("\"generation\":3")!=std::string::npos);
    command("reboot");assert(rebooted && output=="OK\n\n");
    // Discard warm state; verification must use only persistent catalog/records.
    probe_catalog.Invalidate();live_snapshot={};
    selected={};recent={};steps=0;std::memset(payload,0,sizeof(payload));
    command("check");assert(output.find("\"passed\":true")!=std::string::npos);
    command("live_check");assert(output.find("\"passed\":true")!=std::string::npos);
    auto before_erases=erases;
    command("check");assert(output.find("\"passed\":true")!=std::string::npos && erases==before_erases);
    command("clean");assert(output.find("\"cleaned\":true")!=std::string::npos);
    for(auto byte:flash)assert(byte==255);
    command("run");assert(output.find("\"passed\":true")!=std::string::npos && output.find("\"cleaned\":true")!=std::string::npos);
    command("hold");assert(output.find("\"passed\":true")!=std::string::npos);
    // Newest committed corruption quarantines fallback; check/clean cannot erase.
    flash[4096+32+16]^=1;
    before_erases=erases;auto before_writes=writes;
    command("check");assert(output.find("\"passed\":false")!=std::string::npos);
    command("clean");assert(output.find("\"passed\":false")!=std::string::npos);
    assert(erases==before_erases && writes==before_writes);
    puts("Checkpoint hardware handler: strict reserved bounds/DMA, streamed indexes, three catalog commits and native coordinator, cold-state reopen, cleanup and quarantine passed");
}
"""


def main():
    stubs = STUBS.replace("uint8_t flash[4096]", "uint8_t flash[49152]").replace(
        "0xfff000", "0xff4000"
    )
    stubs = stubs.replace(
        "assert(at==0xff4000);", "assert(at>=0xff4000 && at<0x1000000 && at%4096==0);"
    )
    stubs = stubs.replace(
        "std::memset(flash,255,sizeof(flash));return 0;",
        "std::memset(flash+(at-0xff4000),255,4096);return 0;",
    )
    stubs = stubs.replace(
        "struct Console { static void Printf",
        'struct Console { static void SendOk(){output+="OK\\n\\n";} static void Printf',
    )
    source = (ROOT / "src/steno_squid/checkpoint_probe.cc").read_text()
    source = "\n".join(
        line for line in source.splitlines() if not line.startswith("#include")
    )
    headers = "\n".join(
        f'#include "{ROOT / "tools/storage" / name}"'
        for name in (
            "native_catalog.h",
            "native_live_journal.h",
            "native_checkpoint_job.h",
            "native_checkpoint.h",
            "native_index_sink.h",
            "binary_record.h",
        )
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "dictionary").mkdir()
        (path / "dictionary/dictionary.h").write_text(
            (ROOT / "tools/tests/native_dictionary_stub.h").read_text()
        )
        headers += (
            f'\n#include "{ROOT / "src/steno_squid/native_dictionary_adapter.h"}"\n'
        )
        (path / "test.cc").write_text(
            stubs + FRAMEWORK + headers + "\n" + source + HARNESS
        )
        subprocess.run(
            [
                *compiler(),
                "-std=c++17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(path),
                str(path / "test.cc"),
                "-o",
                str(path / "test"),
            ],
            check=True,
        )
        subprocess.run([str(path / "test")], check=True)


if __name__ == "__main__":
    main()
