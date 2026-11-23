def compute_total(items):
    return sum(item.price for item in items)


def tax_for(amount):
    return amount * 0.1
