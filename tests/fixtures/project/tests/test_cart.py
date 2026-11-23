import unittest

from cart import Cart, Item


class TestCart(unittest.TestCase):
    def test_checkout_total(self):
        cart = Cart([Item(50), Item(50)])
        self.assertEqual(cart.checkout(), 100)

    def test_add_item(self):
        cart = Cart([])
        cart.add(Item(25))
        self.assertEqual(len(cart.items), 1)


if __name__ == "__main__":
    unittest.main()
