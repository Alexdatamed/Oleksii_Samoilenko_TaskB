def order_total(quantity, unit_cents, member):
    if quantity < 0 or unit_cents < 0:
        raise ValueError("Amounts and quantities must be nonnegative")
    if quantity == 0:
        return 0
    subtotal = quantity * unit_cents
    if member:
        subtotal = subtotal - (subtotal * 10 + 50) // 100
        if subtotal >= 10000:
            return subtotal
        else:
            return subtotal + 500
    else:
        if subtotal >= 10000:
            return subtotal
        else:
            return subtotal + 500
