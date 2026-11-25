#!/usr/bin/env python3
"""The JavaScript compiler and decoder must stay byte-identical to the Python ones (the Manager's safety net).

Skipped when Node or the pinned dictionaries (tools/dictionaries/prepare.py) are not available.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "build/lapwing/lapwing-base.json", ROOT / "build/plover/plover-main.json",
           ROOT / "build/extras/lapwing-proper-nouns.json", ROOT / "build/extras/lapwing-uk-additions.json"]


@unittest.skipUnless(shutil.which("node"), "Node is not installed")
@unittest.skipUnless(all(path.exists() for path in SOURCES), "run tools/dictionaries/prepare.py first")
class JavaScriptParityTest(unittest.TestCase):
    def run_script(self, name):
        result = subprocess.run([sys.executable, str(ROOT / "tools/bench/js" / name), str(ROOT)],
                                capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("False", result.stdout)

    def test_compiler_output_is_byte_identical(self):
        self.run_script("compare_compile.py")

    def test_decoder_output_is_identical(self):
        self.run_script("compare_decode.py")


if __name__ == "__main__":
    unittest.main()