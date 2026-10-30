"""Floor-anchored gain tests for the OLED Tint effect.

Run from the repo root: python3 -m unittest discover -s tests
"""
import logging
import os
import re
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# `main` imports `decky` at module level and reads `decky.logger` when it
# applies. Set up the same mock as test_main.py, and do it before `import main`
# so that module is bound to the mock no matter which test file loads first.
decky = types.ModuleType("decky")
decky.logger = logging.getLogger("decky-test")
sys.modules["decky"] = decky
import main


def floor_of(fx):
    m = re.search(r"static const float Floor = ([0-9.]+);", fx)
    assert m, "Floor constant missing"
    return float(m.group(1))


def shade(L, Floor, G):
    # The exact HLSL formula, per component (float arithmetic).
    return min(L, Floor) + max(L - Floor, 0.0) * G


class FloorAnchoredGain(unittest.TestCase):
    def setUp(self):
        n = main.CURVE_SIZE
        self.curves = [tuple(i / (n - 1) for i in range(n))] * 3

    def test_no_table_floor_zero_flat_gain(self):
        # 1. No table (legacy soft / outside band): Floor 0, formula == flat gain.
        fx = main.build_fx(self.curves, 0, None, 0.5)
        self.assertEqual(floor_of(fx), 0.0)
        self.assertIn("float3 o = min(L, Floor) + max(L - Floor, 0.0) * G;", fx)

    def test_soft_table_w0_floor_is_v(self):
        # 2. Soft mode with table (w=0): Floor = V/255, identical to the no-table
        #    effect apart from that constant (dither machinery must be absent).
        table = {"V": 16, "cell": 1}
        d0 = {"table": table, "w": 0.0, "temporal": None, "curve": "native"}
        fx = main.build_fx(self.curves, 0, d0, 0.5)
        self.assertAlmostEqual(floor_of(fx), 16 / 255, places=6)
        self.assertEqual(
            fx.replace("Floor = 0.062745", "Floor = 0.000000"),
            main.build_fx(self.curves, 0, None, 0.5),
            "w=0 effect must equal the no-table effect except for Floor",
        )

    def _xs(self):
        return [i / 255 for i in range(256)]

    def test_subfloor_passthrough(self):
        F, G = 16 / 255, 0.5
        self.assertTrue(all(shade(x, F, G) == x for x in self._xs() if x <= F),
                        "sub-floor must pass through")

    def test_floor_point_fixed(self):
        F, G = 16 / 255, 0.5
        self.assertEqual(shade(F, F, G), F, "floor point must be fixed")

    def test_monotone(self):
        F, G = 16 / 255, 0.5
        xs = self._xs()
        self.assertTrue(
            all(shade(x, F, G) > shade(y, F, G) for x, y in zip(xs[1:], xs)
                if x > F >= y or x > y > F),
            "must stay monotone",
        )

    def test_gain_lt1_only_darkens(self):
        F, G = 16 / 255, 0.5
        self.assertTrue(all(shade(x, F, G) <= x for x in self._xs()),
                        "gain<1 must only darken")

    def test_g1_identity(self):
        F = 16 / 255
        self.assertTrue(all(shade(x, F, 1.0) == x for x in self._xs()),
                        "G=1 (dither path) must be identity")

    def test_floor_zero_equals_flat_gain(self):
        G = 0.5
        self.assertTrue(all(shade(x, 0.0, G) == x * G for x in self._xs()),
                        "Floor=0 must equal old flat gain")


if __name__ == "__main__":
    unittest.main()
