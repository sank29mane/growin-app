class CoreMLRunner:
    """Lightweight Core ML runner for on-device inference with ANE support."""
    def __init__(self, model_path: str | None = None):
        self.model_path = model_path
        self._available = False
        self._model = None
        try:
            import coremltools as ct  # type: ignore
            self._ct = ct
            # Set compute units for Neural Engine priority
            self._compute_units = ct.ComputeUnit.ALL  # Prioritizes ANE on Apple Silicon
            self._available = False  # wait until model is loaded
        except Exception:
            self._ct = None
            self._available = False

    def load(self, model_path: str) -> bool:
        if self._ct is None:
            return False
        try:
            self.model_path = model_path
            # Load Core ML model with ANE compute units
            config = self._ct.MLModelConfiguration()
            config.computeUnits = self._compute_units
            self._model = self._ct.models.MLModel(model_path, config=config)
            self._available = True
            return True
        except Exception:
            self._available = False
            return False

    def predict(self, features: dict) -> dict:
        if not self._available or self._model is None:
            raise RuntimeError("Core ML model not loaded or Core ML runtime unavailable")

        try:
            # Run inference on ANE when available
            prediction = self._model.predict(features)
            return prediction
        except Exception as e:
            raise RuntimeError(f"Core ML prediction failed: {e}")

    @property
    def available(self) -> bool:
        return self._available

    def calculate_indicators(self, closes: list[float], highs: list[float] | None = None,
                           lows: list[float] | None = None) -> dict:
        """Calculate technical indicators using Core ML if available."""
        if not self._available:
            return {"error": "Core ML not available"}

        # Prepare input for indicator model
        input_data = {"closes": closes}
        if highs:
            input_data["highs"] = highs
        if lows:
            input_data["lows"] = lows

        try:
            result = self.predict(input_data)
            return result
        except Exception as e:
            return {"error": str(e)}
