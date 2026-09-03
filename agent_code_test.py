def calculate_total(price, quantity):
    return float(price) * quantity


price = "10"
quantity = 5

total = calculate_total(price, quantity)

print(f"Total: £{total:.2f}")
