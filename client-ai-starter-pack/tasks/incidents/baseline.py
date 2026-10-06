def unit_price(total_cents, quantity):
    if total_cents < 0 or quantity < 0:
        raise ValueError("Amounts and quantities must be nonnegative")
    return (2 * total_cents + quantity) // (2 * quantity)
