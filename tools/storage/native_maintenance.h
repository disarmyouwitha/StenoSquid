#pragma once
namespace candidate_b {
enum class JobResult { More, Done, Invalid, Busy, NoSpace, IoError, Corrupt,
                       ReadOnly, RecoveryRequired, Cancelled };
}
