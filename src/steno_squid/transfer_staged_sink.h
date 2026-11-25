#pragma once
/* Transfer Sink over the existing staged update writer (NativeStage).
 *
 * The planning handshake stays on the legacy console: `native_install update
 * begin <kind>` still activates the staged update and returns the root and
 * protected-segment map the Manager plans against. The binary session then
 * ATTACHES to that active update and replaces only the slow erase/write/commit
 * round trips. Nothing here changes staging, allocation or on-flash formats.
 *
 * Stream payload = a sequence of records (the same operations the Manager sends
 * today as `erase N` / `write ADDR BASE64`):
 *   0x01 ERASE  segment u16
 *   0x02 WRITE  address u32, length u16, data[length]
 * WRITE obeys the staged writer's own rules (256-byte aligned address, length a
 * multiple of 4, at most kMaxWrite bytes, inside one erased segment); the
 * backend enforces them and any refusal fails the session.
 *
 * BEGIN args (12 bytes, little-endian): commit segment u32, commit bytes u32,
 * commit CRC-32 u32 - the `commit SEGMENT SIZE CRC` the Manager sends today.
 * BEGIN kind must equal the active staged update's kind.
 *
 * Backend requirements (NativeStage supplies these; firmware wraps it):
 *   bool Active() const;            uint8_t KindId() const;
 *   uint32_t Epoch() const;         // changes every time a staged update begins
 *   bool Erase(uint32_t segment);   bool Write(uint32_t address, const uint8_t *, uint32_t);
 *   bool Read(uint32_t address, uint8_t *, uint32_t);
 *   StagedCommit Commit(uint32_t segment, uint32_t bytes, uint32_t crc, bool &reopen);
 *   void Abort();
 * Firmware owns the surrounding policy: connection-ownership check, flash lock,
 * input pause, reboot/reopen after a successful commit (see reopen_required()).
 *
 * RAM: one kMaxWrite page buffer plus ~48 bytes of state; no heap.
 */
#include "transfer_session.h"

namespace steno_transfer {

enum class StagedCommit : uint8_t { kOk, kBusy, kQuarantined, kRefused };

constexpr uint8_t kRecordErase = 0x01;
constexpr uint8_t kRecordWrite = 0x02;
constexpr size_t kMaxWrite = 256;
constexpr size_t kStagedArgsBytes = 12;
constexpr size_t kVerifySliceBytes = 4096; // bounded work per Verify call

template <class Backend>
class StagedSink final : public Sink {
public:
    explicit StagedSink(Backend &backend) : backend_(backend) {}

    bool reopen_required() const { return reopen_; }
    StagedCommit commit_status() const { return commit_status_; }
    uint32_t writes() const { return writes_; }
    uint32_t erases() const { return erases_; }

    SinkResult Begin(const BeginInfo &info) override {
        attached_ = false;
        reopen_ = false;
        commit_status_ = StagedCommit::kOk;
        if (info.args_length != kStagedArgsBytes || !backend_.Active() || backend_.KindId() != info.kind)
            return SinkResult::kFail;
        epoch_ = backend_.Epoch();
        commit_segment_ = Get32(info.args);
        commit_bytes_ = Get32(info.args + 4);
        commit_crc_ = Get32(info.args + 8);
        // No upper segment bound here: the pool is 255 segments on 16 MiB and 1023 on 64 MiB, and the staged writer
        // validates every segment against the real geometry (NativePoolEnd), so the adapter must not hard-code it.
        if (commit_segment_ < 2 || commit_bytes_ == 0) return SinkResult::kFail;
        state_ = Parse::kType;
        need_ = used_ = 0;
        writes_ = erases_ = 0;
        commit_erased_ = false;
        verify_offset_ = 0;
        verify_crc_ = 0;
        attached_ = true; // From here Abort() releases the staged update.
        return SinkResult::kOk;
    }

    SinkResult Write(uint32_t, const uint8_t *data, size_t size) override {
        if (!Valid()) return SinkResult::kFail;
        while (size) {
            switch (state_) {
            case Parse::kType:
                if (*data == kRecordErase) { state_ = Parse::kEraseBody; need_ = 2; }
                else if (*data == kRecordWrite) { state_ = Parse::kWriteHeader; need_ = 6; }
                else return SinkResult::kFail;
                used_ = 0;
                data++; size--;
                break;
            case Parse::kEraseBody:
            case Parse::kWriteHeader: {
                const size_t take = Take(data, size);
                data += take; size -= take;
                if (used_ < need_) break;
                if (state_ == Parse::kEraseBody) {
                    const uint32_t segment = Get16(page_);
                    if (!backend_.Erase(segment)) return SinkResult::kFail;
                    erases_++;
                    commit_erased_ |= segment == commit_segment_;
                    state_ = Parse::kType;
                } else {
                    address_ = Get32(page_);
                    need_ = Get16(page_ + 4);
                    if (!need_ || need_ > kMaxWrite || need_ % 4 || address_ % 256) return SinkResult::kFail;
                    state_ = Parse::kWriteData;
                    used_ = 0;
                }
                break;
            }
            case Parse::kWriteData: {
                const size_t take = Take(data, size);
                data += take; size -= take;
                if (used_ < need_) break;
                if (!backend_.Write(address_, page_, static_cast<uint32_t>(need_))) return SinkResult::kFail;
                writes_++;
                state_ = Parse::kType;
                break;
            }
            }
        }
        return SinkResult::kOk;
    }

    /* Flash readback of the commit payload, in bounded slices (kBusy until done). */
    SinkResult Verify() override {
        if (!Valid() || state_ != Parse::kType || !commit_erased_) return SinkResult::kFail;
        uint8_t *const block = page_; // Parsing is idle here, so the record buffer is free: no stack cost.
        const uint32_t base = commit_segment_ * kSegmentBytes;
        uint32_t budget = kVerifySliceBytes;
        while (verify_offset_ < commit_bytes_ && budget) {
            const uint32_t n = commit_bytes_ - verify_offset_ < kMaxWrite ? commit_bytes_ - verify_offset_ : kMaxWrite;
            if (!backend_.Read(base + verify_offset_, block, n)) return SinkResult::kFail;
            verify_crc_ = Crc32Update(verify_crc_, block, n);
            verify_offset_ += n;
            budget = budget > n ? budget - n : 0;
        }
        if (verify_offset_ < commit_bytes_) return SinkResult::kBusy;
        return verify_crc_ == commit_crc_ ? SinkResult::kOk : SinkResult::kFail;
    }

    SinkResult Commit() override {
        if (!Valid()) return SinkResult::kFail;
        bool reopen = false;
        commit_status_ = backend_.Commit(commit_segment_, commit_bytes_, commit_crc_, reopen);
        reopen_ = reopen;
        if (commit_status_ == StagedCommit::kBusy) return SinkResult::kBusy;
        if (commit_status_ != StagedCommit::kOk) return SinkResult::kFail;
        attached_ = false; // Published; the firmware wrapper now finishes/reopens.
        return SinkResult::kOk;
    }

    void Abort() override {
        // Never abort a different update that began after ours was lost (timeout, legacy abort).
        if (attached_ && backend_.Epoch() == epoch_) backend_.Abort();
        attached_ = false;
        state_ = Parse::kType;
    }

private:
    static constexpr uint32_t kSegmentBytes = 65536;
    enum class Parse : uint8_t { kType, kEraseBody, kWriteHeader, kWriteData };

    bool Valid() const { return attached_ && backend_.Active() && backend_.Epoch() == epoch_; }

    size_t Take(const uint8_t *data, size_t size) {
        const size_t room = need_ - used_;
        const size_t take = size < room ? size : room;
        memcpy(page_ + used_, data, take);
        used_ += take;
        return take;
    }

    Backend &backend_;
    Parse state_ = Parse::kType;
    bool attached_ = false, commit_erased_ = false, reopen_ = false;
    StagedCommit commit_status_ = StagedCommit::kOk;
    uint32_t commit_segment_ = 0, commit_bytes_ = 0, commit_crc_ = 0;
    uint32_t epoch_ = 0, address_ = 0, verify_offset_ = 0, verify_crc_ = 0, writes_ = 0, erases_ = 0;
    size_t need_ = 0, used_ = 0;
    uint8_t page_[kMaxWrite]; // Also holds record headers (6 bytes).
};

} // namespace steno_transfer
