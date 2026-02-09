import platform
import sys

def detect_ane_available() -> bool:
    """Detect if Apple Neural Engine (ANE) is available on this host.
    
    Checks for macOS + Apple Silicon (arm64).
    Targeting M1/M2/M3/M4 chips which all have ANE.
    """
    try:
        if sys.platform != "darwin":
            return False
            
        # Check architecture
        machine = platform.machine()
        processor = platform.processor()
        
        is_arm = machine == "arm64" or processor == "arm"
        
        if is_arm:
            # Further validation could be done via sysctl but this is sufficient for
            # assuming NPU presence on Mac.
            return True
            
        return False
    except Exception:
        return False

__all__ = ["detect_ane_available"]
