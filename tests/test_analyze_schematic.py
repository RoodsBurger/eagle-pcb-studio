#!/usr/bin/env python3
"""analyze_schematic: library-integrity findings for a schematic whose device connects name pads the package lacks."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, HERE)
import analyze_schematic  # noqa: E402
from test_sch_to_board import SCH  # noqa: E402


class LibraryIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sch = os.path.join(self.tmp, "t.sch")
        with open(self.sch, "w") as f:
            f.write(SCH)

    def test_connect_pad_missing_is_an_error(self):
        res = analyze_schematic.analyze(self.sch)
        ids = [f["id"] for f in res["findings"]]
        self.assertIn("lib.connect_pad_missing", ids)
        f = next(f for f in res["findings"] if f["id"] == "lib.connect_pad_missing")
        self.assertEqual(f["severity"], "ERROR")
        self.assertIn("2B", f["message"])
        self.assertIn("2C", f["message"])

    def test_clean_schematic_has_no_library_findings(self):
        clean = SCH.replace('pad="2 2B 2C"', 'pad="2"')
        with open(self.sch, "w") as f:
            f.write(clean)
        res = analyze_schematic.analyze(self.sch)
        self.assertFalse([f for f in res["findings"] if f["id"].startswith("lib.") or f["id"].startswith("net.pinref")])

    def test_pinref_to_unknown_part_and_pin(self):
        broken = SCH.replace('<pinref part="R2" gate="G$1" pin="A"/>', '<pinref part="R9" gate="G$1" pin="A"/><pinref part="R2" gate="G$1" pin="Q"/>')
        with open(self.sch, "w") as f:
            f.write(broken)
        ids = [f["id"] for f in analyze_schematic.analyze(self.sch)["findings"]]
        self.assertIn("net.pinref_unknown_part", ids)
        self.assertIn("net.pinref_unknown_pin", ids)


if __name__ == "__main__":
    unittest.main()
