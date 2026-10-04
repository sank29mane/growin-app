"""VM-only relay package for the Growin Breeze gateway.

Standard library plus FastAPI. It must never import the vendor SDK: the
package talks to the broker with its own thin client and documented checksum.
"""

__version__ = "61.0.0"
