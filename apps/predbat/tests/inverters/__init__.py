# -----------------------------------------------------------------------------
# Unified inverter test registry
# -----------------------------------------------------------------------------
from tests.inverters.ge import DEFINITION as GE_DEFINITION

# from tests.inverters.sig import DEFINITION as SIG_DEFINITION  # pending SIG control PR

ALL_INVERTERS = [
    GE_DEFINITION,
    # SIG_DEFINITION,
]
