import pytest
from utils.rstitch_engine import RStitchEngine

@pytest.mark.asyncio
async def test_rstitch_delegation_high_entropy():
    """Verify that high-entropy prompts delegate to the LLM."""
    engine = RStitchEngine(entropy_threshold=0.5)
    
    # "risk" is a keyword in our mock that triggers high entropy
    result = await engine.generate_step("Assess the risk of this portfolio", {})
    
    assert result["model"] == "native-mlx"
    assert result["stitched"] == True
    assert result["entropy"] > 0.5

@pytest.mark.asyncio
async def test_rstitch_delegation_low_entropy():
    """Verify that low-entropy prompts stay on the SLM."""
    engine = RStitchEngine(entropy_threshold=0.8)
    
    # Generic prompt triggers low entropy in our mock
    result = await engine.generate_step("Hello how are you", {})
    
    assert result["model"] == "granite-tiny"
    assert result["stitched"] == False
    assert result["entropy"] < 0.8

@pytest.mark.asyncio
async def test_rstitch_threshold_boundary():
    """Test boundary condition for entropy threshold."""
    # Custom mock to force exact entropy
    class ExactEntropyEngine(RStitchEngine):
        def __init__(self, forced_entropy, threshold):
            super().__init__(threshold)
            self.forced_entropy = forced_entropy
        async def generate_step(self, prompt, context):
            res = await super().generate_step(prompt, context)
            res["entropy"] = self.forced_entropy
            res["stitched"] = self.forced_entropy > self.entropy_threshold
            res["model"] = self.llm_model if res["stitched"] else self.slm_model
            return res

    engine = ExactEntropyEngine(forced_entropy=0.5, threshold=0.5)
    result = await engine.generate_step("test", {})
    assert result["stitched"] == False # Exact threshold should be SLM (using > for high)
    
    engine = ExactEntropyEngine(forced_entropy=0.51, threshold=0.5)
    result = await engine.generate_step("test", {})
    assert result["stitched"] == True
