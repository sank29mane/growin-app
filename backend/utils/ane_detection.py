import platform
import sys

def detect_ane_available() -> bool:
    """Detect if Apple Neural Engine (ANE) is available on this host.

    Heuristic: macOS on Apple Silicon (arm64) is considered ANE-capable.
    This does not guarantee runtime availability, but provides a sane default.
    """
    try:
        if sys.platform != "darwin":
            return False
        arch = platform.machine()
        return arch == "arm64"
    except Exception:
        return False

__all__ = ["detect_ane_available"]
