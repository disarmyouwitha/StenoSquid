#include "native_store_fixture.h"
#include "native_provisioner.h"
#include <iostream>
int main() {
    Nor nor;nor.data.resize(9*65536,0);NativeDevice device{{nor}};
    NativeProvisioner<NativeDevice,8> install(device);
    std::vector<uint8_t> expected(8*65536,255);uint8_t page[512];std::memset(page,0x5a,sizeof(page));
    std::memcpy(expected.data()+3*65536,page,sizeof(page));const auto crc=Crc(0,expected.data(),expected.size());
    assert(install.Begin(crc)==ProvisionResult::Ok && install.Begin(crc)==ProvisionResult::Busy);
    assert(install.Write(0,page,512)==ProvisionResult::Incomplete && install.Finish()==ProvisionResult::Incomplete);
    for(unsigned i=0;i<8;++i)assert(install.Erase(i)==ProvisionResult::Ok);
    assert(install.Erase(8)==ProvisionResult::Invalid);
    assert(install.Write(3*65536,page,512)==ProvisionResult::Ok);
    const auto ops=nor.operations;
    assert(install.Write(3*65536,page,512)==ProvisionResult::Ok && nor.operations==ops);
    assert(install.Erase(3)==ProvisionResult::Ok && nor.operations==ops);
    assert(install.Write(8*65536,page,512)==ProvisionResult::Invalid);
    assert(install.Write(1,page,512)==ProvisionResult::Invalid);
    uint8_t conflict[512];std::memset(conflict,0xa5,sizeof(conflict));
    assert(install.Write(3*65536,conflict,512)==ProvisionResult::Invalid);
    assert(install.Finish()==ProvisionResult::Ok && install.Finish()==ProvisionResult::Ok);
    assert(std::equal(expected.begin(),expected.end(),nor.data.begin()));
    for(unsigned i=8*65536;i<9*65536;++i)assert(!nor.data[i]);
    assert(install.Begin(crc+1)==ProvisionResult::Ok);
    for(unsigned i=0;i<8;++i)assert(install.Erase(i)==ProvisionResult::Ok);
    assert(install.Finish()==ProvisionResult::ChecksumMismatch);install.Abort();
    assert(!install.Active() && install.Write(0,page,512)==ProvisionResult::Invalid);
    std::cout<<"Native clean provisioning: erase ordering, retry idempotence, readback, checksum refusal and reserved-tail preservation passed\n";
}
