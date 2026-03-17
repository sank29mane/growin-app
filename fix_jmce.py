with open("backend/utils/jmce_model.py", "r") as f:
    code = f.read()

# Replace mx.array typing hints with string literal 'mx.array'
code = code.replace("mu: mx.array) -> mx.array:", "mu: 'mx.array') -> 'mx.array':")
code = code.replace("x: mx.array,", "x: 'mx.array',")
code = code.replace("error_vector: Optional[mx.array] = None,", "error_vector: Optional['mx.array'] = None,")
code = code.replace(") -> Tuple[mx.array, mx.array, Optional[mx.array]]:", ") -> Tuple['mx.array', 'mx.array', Optional['mx.array']]:")
code = code.replace("def _build_cholesky(self, L_flat: mx.array) -> mx.array:", "def _build_cholesky(self, L_flat: 'mx.array') -> 'mx.array':")
code = code.replace("def get_covariance(self, L: mx.array) -> mx.array:", "def get_covariance(self, L: 'mx.array') -> 'mx.array':")

with open("backend/utils/jmce_model.py", "w") as f:
    f.write(code)
