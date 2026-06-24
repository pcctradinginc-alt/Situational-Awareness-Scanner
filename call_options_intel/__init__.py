"""
call_options_intel
===================

Free-data, research-only CALL-options intelligence subsystem for the
Situational Awareness Scanner.

It ranks *explainable* CALL-option candidates on AI-infrastructure equities by
combining:

  * a structured thesis vector (Aschenbrenner / Thiel / Shulman themes),
  * SEC 13F smart-money accumulation,
  * free market-data momentum/trend context,
  * free options-chain liquidity / IV-regime context,
  * catalyst proximity (earnings, capex, 13F changes),

minus an explicit risk penalty. It NEVER executes trades and requires NO paid
APIs. Missing data degrades gracefully and is flagged, never fatal.
"""

__version__ = "0.1.0"

DISCLAIMER = (
    "RESEARCH / PAPER MODE ONLY — no live trading. Not investment advice. "
    "Options can expire worthless (total loss). Scores are explainable "
    "heuristics, not probabilities of profit."
)
