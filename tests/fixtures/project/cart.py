import pricing


class Item:
    def __init__(self, price):
        self.price = price


class Cart:
    def __init__(self, items):
        self.items = items

    def add(self, item):
        self.items.append(item)

    def checkout(self):
        # total via the pricing module
        return pricing.total_price(self.items)
