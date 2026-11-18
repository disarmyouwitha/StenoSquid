"""Pinned dictionary inputs and the shared external-flash layout."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
XIP_BASE = 0x12000000
PERSONAL_START = 0x500000
PERSONAL_SEGMENTS = 175
SEGMENT_BYTES = 0x10000
CHUNK_BYTES = 512
LAPWING_COMMIT = "4ac5d53cf039a2b1f4c484456663c85e7df4e859"
LAPWING_URL = f"https://raw.githubusercontent.com/aerickt/plover-lapwing-aio/{LAPWING_COMMIT}/plover_lapwing/dictionaries/"
PLOVER_COMMIT = "8a52c5296c502507f9a46820a4a947ca25f998e6"
# Ordered as in the firmware's extras_names.
EXTRAS = [
    ("lapwing-commands.json", "Lapwing Commands", "6160e19084c77381f66aeda72473d807bcf3ea7f0c6303a37189ca60bf59ff3d"),
    ("lapwing-numbers.json", "Lapwing Numbers", "d104b9d6f8ea7935bdf3129d69bcdc4100e2b55fa717b41be2307043a4bd4b73"),
    ("lapwing-uk-additions.json", "Lapwing UK Additions", "e6d3190d0417c9912b34ebf9a3d02be08935d5c1c1b46bb47f0b580ed21717d3"),
    ("lapwing-proper-nouns.json", "Lapwing Proper Nouns", "aef0205fa4bc6bcb1bc11c199b0c01737ada5ffa7a0f0a56d48154d024ce66e8"),
]
BASES = {
    "LAPWING": {"directory": "lapwing", "image": "lapwing-base.bin", "offset": 0,
                "sources": [(LAPWING_URL + "lapwing-base.json", "lapwing-base.json", "lapwing-base", "2750c20466bc5040d69167e02095776531afb6c3c1f6f662bba01006e7e0e484")]},
    "PLOVER": {"directory": "plover", "image": "plover-main.bin", "offset": 0x200000,
               "sources": [(f"https://raw.githubusercontent.com/openstenoproject/plover/{PLOVER_COMMIT}/plover/assets/main.json", "plover-main.json", "Plover Base", "6570873c7f703ecd39c9870083f643db3475b49462a8db80fe22fe33ac3cfe92")]},
    "EXTRAS": {"directory": "extras", "image": "lapwing-extras.bin", "offset": 0x480000,
               "sources": [(LAPWING_URL + file, file, name, sha) for file, name, sha in EXTRAS]},
}
