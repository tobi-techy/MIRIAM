"""Investment action layer (spec §16-§18).

Miriam is the intelligence; this package is the hand that executes -- through a
provider adapter, behind a policy gate, and only ever on an explicit user
confirmation tied to the exact proposed action.
"""

from miriam_agent.investments.provider import (  # noqa: F401
    GliderInvestmentProvider,
    InvestmentProvider,
    MockInvestmentProvider,
    UnsupportedOperationError,
)