"""Structure-aware AI trading pipeline.

Market data -> structure/indicator engines -> Claude Fable 5 multi-agent
reasoning -> deterministic safety layer -> TradersPost execution -> position
monitoring with dynamic stops.
"""

__version__ = "0.1.0"
