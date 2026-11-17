#!/usr/bin/env python3
"""Export bytes are sent with the engine lock released and writer scopes restored."""
from hostlib import compiler
from pathlib import Path
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[2]
CONSOLE=r'''
#pragma once
#include <stddef.h>
class IWriter {public: virtual void Write(const char *,size_t)=0;};
class ConsoleWriter {public:
 static inline IWriter *active;
 static inline IWriter *stack[4];
 static inline unsigned count;
 static IWriter *GetActiveWriter(){return active;}
 static void Push(IWriter *writer){stack[count++]=active;active=writer;}
 static void Pop(){active=stack[--count];}
};
'''
CODE=r'''
#include <cassert>
#include <string>
#include "engine_stream.h"
static bool held=true;
static unsigned yields;
static IWriter *logger;
extern "C" void lapwing_flash_release(void){assert(held && ConsoleWriter::count==0 && ConsoleWriter::active==logger);held=false;yields++;}
extern "C" bool lapwing_flash_resume_read(void){assert(!held);held=true;return true;}
class Sink final:public IWriter {public:
 std::string bytes;
 bool expect_locked;
 explicit Sink(bool value):expect_locked(value){}
 void Write(const char *text,size_t n) override {assert(held==expect_locked);bytes.append(text,n);}
};
int main(){
 Sink log(true),host(false);logger=&log;ConsoleWriter::active=logger;
 ConsoleWriter::Push(&host);
 const std::string text(1000,'x');
 {StenoSquidExportWriter writer(true);writer.Write(text.data(),text.size());assert(held && ConsoleWriter::count==2);}
 assert(host.bytes==text && yields==16 && held && ConsoleWriter::count==1 && ConsoleWriter::active==&host);
 ConsoleWriter::Pop();assert(ConsoleWriter::active==logger);
 Sink internal(true);ConsoleWriter::Push(&internal);
 {StenoSquidExportWriter writer(false);writer.Write("internal",8);}
 assert(internal.bytes=="internal" && ConsoleWriter::count==1);ConsoleWriter::Pop();
}
'''

def main():
 with tempfile.TemporaryDirectory() as directory:
  root=Path(directory);(root/'console.h').write_text(CONSOLE,encoding='utf-8');(root/'test.cc').write_text(CODE,encoding='utf-8')
  subprocess.run([*compiler(),'-std=c++17','-Wall','-Wextra','-Werror','-I',str(root),'-I',str(ROOT/'src/steno_squid'),str(root/'test.cc'),'-o',str(root/'test')],check=True)
  subprocess.run([str(root/'test')],check=True)
 print('PASS: export chunking releases the engine lock and restores writer scopes byte-for-byte')

if __name__=='__main__':main()
