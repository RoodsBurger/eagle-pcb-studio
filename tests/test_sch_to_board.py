#!/usr/bin/env python3
"""sch_to_board: a device connect naming a pad the embedded package lacks must not become a contactref."""
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import sch_to_board  # noqa: E402

SCH = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE eagle SYSTEM "eagle.dtd">
<eagle version="9.6.2">
<drawing>
<schematic>
<libraries>
<library name="lib">
<packages>
<package name="P2">
<smd name="1" x="-1" y="0" dx="1" dy="1" layer="1"/>
<smd name="2" x="1" y="0" dx="1" dy="1" layer="1"/>
</package>
</packages>
<symbols><symbol name="S"><pin name="A" x="0" y="0"/><pin name="B" x="0" y="-2.54"/></symbol></symbols>
<devicesets>
<deviceset name="D"><gates><gate name="G$1" symbol="S" x="0" y="0"/></gates>
<devices><device name="" package="P2"><connects>
<connect gate="G$1" pin="A" pad="1"/>
<connect gate="G$1" pin="B" pad="2 2B 2C"/>
</connects><technologies><technology name=""/></technologies></device></devices></deviceset>
</devicesets>
</library>
</libraries>
<parts>
<part name="R1" library="lib" deviceset="D" device=""/>
<part name="R2" library="lib" deviceset="D" device=""/>
</parts>
<sheets><sheet><instances/><nets>
<net name="N1"><segment><pinref part="R1" gate="G$1" pin="A"/><pinref part="R2" gate="G$1" pin="A"/></segment></net>
<net name="GND"><segment><pinref part="R1" gate="G$1" pin="B"/><pinref part="R2" gate="G$1" pin="B"/></segment></net>
</nets></sheet></sheets>
</schematic>
</drawing>
</eagle>
"""


class DanglingConnectPads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sch = os.path.join(self.tmp, "t.sch")
        with open(self.sch, "w") as f:
            f.write(SCH)

    def test_parse_drops_missing_pads_and_reports_them(self):
        parsed = sch_to_board.parse_schematic(self.sch)
        self.assertEqual(sorted(parsed["dangling"]), [("R1", "B", "2B"), ("R1", "B", "2C"), ("R2", "B", "2B"), ("R2", "B", "2C")])
        self.assertEqual(sorted(parsed["signals"]["GND"]), [("R1", "2"), ("R2", "2")])
        self.assertEqual(sorted(parsed["signals"]["N1"]), [("R1", "1"), ("R2", "1")])

    def test_emitted_board_has_only_existing_pads(self):
        out = os.path.join(self.tmp, "t.brd")
        sch_to_board.main([self.sch, "-o", out, "--board", "20x20"])
        root = ET.parse(out).getroot()
        pads = {e.get("name") for p in root.iter("package") for e in p.iter("smd")}
        refs = [(c.get("element"), c.get("pad")) for s in root.iter("signal") for c in s.iter("contactref")]
        self.assertEqual(len(refs), 4)
        self.assertTrue(all(pad in pads for _, pad in refs), refs)
        self.assertTrue(open(out).read().startswith("<?xml"))


if __name__ == "__main__":
    unittest.main()
