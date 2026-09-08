with open('simulation/models.py', 'r') as f:
    content = f.read()

old_block = """        # Walk the book to find average fill price using vectorized numpy operations
        valid_mask = sizes > 0"""

new_block = """        # Walk the book to find average fill price using vectorized numpy operations
        sizes = np.asarray(sizes, dtype=np.float64)
        prices = np.asarray(prices, dtype=np.float64)
        valid_mask = sizes > 0"""

if old_block in content:
    content = content.replace(old_block, new_block)
    with open('simulation/models.py', 'w') as f:
        f.write(content)
    print("Replaced successfully.")
else:
    print("Could not find block.")
