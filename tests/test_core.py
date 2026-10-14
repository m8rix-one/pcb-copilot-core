"""Offline tests: the circuit IR, the schematic writer and the placer, with a fake part library (no KiCad, no
network). Run with:  python -m unittest discover -s tests"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcbcopilot import ir, library, placer, schematic          # noqa: E402
from pcbcopilot.sexpr import Q, find_all, loads                 # noqa: E402

# A two-pin resistor symbol in KiCad's library format, and a three-pin regulator with a power_in pin.
R_SYM = loads('''(symbol "Device:R" (pin_numbers (hide yes)) (pin_names (offset 0))
  (symbol "R_0_1" (rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254)) (fill (type none))))
  (symbol "R_1_1"
    (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))))''')
U_SYM = loads('''(symbol "pcbcopilot:LDO" (pin_names (offset 1.016))
  (symbol "LDO_0_1" (rectangle (start -5.08 -5.08) (end 5.08 5.08) (stroke (width 0.254)) (fill (type none))))
  (symbol "LDO_1_1"
    (pin power_in line (at -7.62 2.54 0) (length 2.54) (name "VIN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin power_in line (at 0 -7.62 90) (length 2.54) (name "GND" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
    (pin power_out line (at 7.62 2.54 180) (length 2.54) (name "VOUT" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))))''')

PARTS = {
    "C23025": {"lcsc": "C23025", "mpn": "0603WAF3000T5E", "package": "0603", "kind": "resistor", "description": "300 ohm",
               "datasheet": "", "symbol": "Device:R", "footprint": "Resistor_SMD:R_0603_1608Metric", "source": "kicad",
               "pins": library.symbol_pins(R_SYM), "basic": True, "specs": {}},
    "C6186": {"lcsc": "C6186", "mpn": "AMS1117-3.3", "package": "SOT-223", "kind": "other", "description": "LDO 3.3V",
              "datasheet": "", "symbol": "pcbcopilot:LDO", "footprint": "pcbcopilot:SOT-223_C6186", "source": "lcsc",
              "pins": library.symbol_pins(U_SYM), "basic": True, "specs": {}, "category": "Power Management"},
}


def fake_get(lcsc, row=None):
    if lcsc not in PARTS:
        raise library.PartError(f"{lcsc} is not in the catalog")
    return PARTS[lcsc]


FLAG_SYM = loads('''(symbol "power:PWR_FLAG" (power) (pin_numbers (hide yes)) (pin_names (offset 0) (hide yes))
  (symbol "PWR_FLAG_0_0" (pin power_out line (at 0 0 90) (length 0) (name "" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27))))))
  (symbol "PWR_FLAG_0_1" (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27)) (stroke (width 0)) (fill (type none)))))''')


def fake_symbol(meta):
    return {"Device:R": R_SYM, "pcbcopilot:LDO": U_SYM}[meta["symbol"]]


def fake_std_symbol(lib_id):
    assert lib_id == "power:PWR_FLAG", lib_id
    return FLAG_SYM


def design():
    d = ir.new("Test board")
    D = ir.Design(d)
    D.add("C6186", ref="U1", purpose="3.3 V regulator")
    D.add("C23025", value="300", purpose="LED resistor")
    D.connect("VIN", ["U1.VIN", "R1.1"], voltage=5)
    D.connect("GND", ["U1.GND"])
    D.connect("+3V3", ["U1.VOUT"], voltage=3.3)
    return D


@mock.patch.object(library, "get", fake_get)
@mock.patch.object(library, "symbol_sexpr", fake_symbol)
@mock.patch.object(schematic.env, "std_symbol", fake_std_symbol)
class IRTests(unittest.TestCase):
    def test_pins_resolve_by_name_and_number(self):
        D = design()
        self.assertEqual(D.resolve_pins("U1.VOUT"), ["U1.3"])
        self.assertEqual(D.resolve_pins("U1.2"), ["U1.2"])
        self.assertEqual(D.net("VIN")["pins"], ["U1.1", "R1.1"])

    def test_an_invented_pin_is_refused(self):
        D = design()
        with self.assertRaises(ir.IRError) as cm:
            D.connect("X", ["U1.SDA"])
        self.assertIn("has no pin 'SDA'", str(cm.exception))
        self.assertIsNone(D.net("X"))

    def test_a_pin_cannot_join_two_nets(self):
        D = design()
        with self.assertRaises(ir.IRError):
            D.connect("OTHER", ["U1.VIN"])
        self.assertEqual(ir.validate(D.d), [])

    def test_remove_cleans_nets(self):
        D = design()
        D.remove("R1")
        self.assertEqual(D.net("VIN")["pins"], ["U1.1"])
        self.assertEqual(D.next_ref("R"), "R1")

    def test_schematic_has_a_label_per_connected_pin_and_a_flag_per_open_pin(self):
        D = design()
        D.mark_nc(["R1.2"])
        text, info = schematic.build(D, "board")
        sch = loads(text)
        self.assertEqual(sch[0], "kicad_sch")
        labels = sorted(str(g[1]) for g in find_all(sch, "global_label"))
        # one label per connected pin, plus a PWR_FLAG label on VIN and GND (+3V3 is driven by U1's VOUT)
        self.assertEqual(labels, ["+3V3", "GND", "GND", "VIN", "VIN", "VIN"])
        self.assertEqual(len(find_all(sch, "no_connect")), 1)
        symbols = find_all(sch, "symbol")
        self.assertEqual(len(symbols), 4)
        self.assertEqual(sum(1 for s in symbols if str(s[1][1]) == "power:PWR_FLAG"), 2)
        self.assertEqual(set(info["paths"]), {"U1", "R1"})
        self.assertEqual(info["paper"], "A4")
        lib_ids = {str(s[1]) for s in find_all(sch, "lib_symbols")[0][1:]}
        self.assertEqual(lib_ids, {"Device:R", "pcbcopilot:LDO", "power:PWR_FLAG"})

    def test_placer_keeps_parts_inside_the_board(self):
        D = design()
        geom = {"U1": {"courtyard": [-4, -4, 4, 4]}, "R1": {"courtyard": [-1, -0.6, 1, 0.6]}}
        pl = placer.place(D, geom, {"edges": {"U1": "left"}})
        self.assertEqual(set(pl["parts"]), {"U1", "R1"})
        for ref, p in pl["parts"].items():
            x0, y0, x1, y1 = geom[ref]["courtyard"]
            self.assertGreaterEqual(p["x"] + x0, 0)
            self.assertGreaterEqual(p["y"] + y0, 0)
            self.assertLessEqual(p["x"] + x1, pl["width"])
            self.assertLessEqual(p["y"] + y1, pl["height"])
        self.assertAlmostEqual(pl["parts"]["U1"]["x"] - 4, placer.EDGE)


class SexprTests(unittest.TestCase):
    def test_round_trip(self):
        from pcbcopilot.sexpr import dumps
        e = ["a", Q("quoted \"x\""), 1, 2.5, ["b", "c"]]
        self.assertEqual(loads(dumps(e)), e)


if __name__ == "__main__":
    unittest.main()
