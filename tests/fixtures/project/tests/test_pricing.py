import unittest

import pricing
from cart import Item


class TestPricing(unittest.TestCase):
    def test_compute_total(self):
        self.assertEqual(pricing.compute_total([Item(30), Item(70)]), 100)

    def test_tax_for(self):
        self.assertAlmostEqual(pricing.tax_for(200), 20.0)


if __name__ == "__main__":
    unittest.main()
