"""
Structure definitions, in the sign conventions the existing engine already uses.

    SPREAD_GiGj        Gi - Gj                weights [+1, -1]
    FLY_GiGjGk         Gi - 2Gj + Gk          weights [+1, -2, +1]
    RATIO_12_GiGjGk    near - 2 x next        weights [+1, -3, +2]
    RATIO_23_GiGjGk    2 x near - 3 x next    weights [+2, -5, +3]

A SPREAD is therefore NEGATIVE in contango, and the carry trade is to sell it.

Every structure carries its own cost, because the leg count is what you pay:
gross contracts x the per-contract round-trip commission. At this desk's rate
that is $5 / $10 / $15 / $25 for a spread / fly / 1:2 / 2:3.

Values and P&L are quoted in TICKS throughout: one tick is 0.01 index points of
the weighted sum, which is $10 on one structure unit at the VX $1,000 multiplier.
This is the same 100x convention as `phase2_structure_engine`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TICK_VALUE_USD = 10.0            # 0.01 index points x $1,000 multiplier
COMMISSION_PER_CONTRACT_RT = 2.50  # dollars, round trip

FAMILY_SPREAD = "1:1 calendar"
FAMILY_FLY = "fly 1:2:1"
FAMILY_SKIPFLY = "skip-fly"
FAMILY_R12 = "ratio 1:2"
FAMILY_R23 = "ratio 2:3"

# Families whose weighting is an exchange-recognised spread type, so the package
# can be worked at the 0.01 net-price increment instead of legged.
RECOGNISED = {FAMILY_SPREAD, FAMILY_FLY, FAMILY_SKIPFLY}


@dataclass(frozen=True)
class Structure:
    name: str
    legs: tuple[int, ...]
    weights: tuple[int, ...]
    family: str
    signal_traded: bool = True      # is this a hunting-book candidate?
    carry_traded: bool = False      # is this a hold-and-roll candidate?
    note: str = ""

    @property
    def gross_contracts(self) -> int:
        return sum(abs(w) for w in self.weights)

    @property
    def n_legs(self) -> int:
        return len(self.legs)

    @property
    def recognised_spread(self) -> bool:
        return self.family in RECOGNISED

    @property
    def cost_usd(self) -> float:
        return self.gross_contracts * COMMISSION_PER_CONTRACT_RT

    @property
    def cost_ticks(self) -> float:
        return self.cost_usd / TICK_VALUE_USD

    def value(self, prices: dict[int, float]) -> float | None:
        """Structure value in ticks from a {generic: price} mapping."""
        try:
            return sum(w * prices[g] for w, g in zip(self.weights, self.legs)) * 100.0
        except (KeyError, TypeError):
            return None


def _build(max_generic: int = 8) -> dict[str, Structure]:
    s: dict[str, Structure] = {}

    def add(st: Structure):
        s[st.name] = st

    # --- adjacent 1:1 calendars -------------------------------------------
    for i in range(1, max_generic):
        add(Structure(f"SPREAD_G{i}G{i+1}", (i, i + 1), (1, -1), FAMILY_SPREAD,
                      signal_traded=False, carry_traded=True,
                      note="carry vehicle; deviation signal subtracts"))

    # --- wide 1:1 calendars: the carry book -------------------------------
    for a, b in ((1, 3), (1, 4), (1, 6), (2, 4), (2, 5), (3, 5)):
        if b <= max_generic:
            add(Structure(f"SPREAD_G{a}G{b}", (a, b), (1, -1), FAMILY_SPREAD,
                          signal_traded=False, carry_traded=True,
                          note="widest carry, largest slippage tolerance"))

    # --- adjacent butterflies ---------------------------------------------
    for i in range(1, max_generic - 1):
        add(Structure(f"FLY_G{i}G{i+1}G{i+2}", (i, i + 1, i + 2), (1, -2, 1),
                      FAMILY_FLY, signal_traded=True, carry_traded=False,
                      note="pure curvature; signal vehicle"))

    # --- skip butterflies -------------------------------------------------
    for a, b, c in ((1, 3, 5), (2, 4, 6), (1, 4, 7)):
        if c <= max_generic:
            add(Structure(f"SKIPFLY_G{a}G{b}G{c}", (a, b, c), (1, -2, 1),
                          FAMILY_SKIPFLY, signal_traded=False, carry_traded=True,
                          note="carries like a wide calendar; no signal edge"))

    # --- 1:2 and 2:3 ratio structures -------------------------------------
    for i in range(1, max_generic - 1):
        add(Structure(f"RATIO_12_G{i}G{i+1}G{i+2}", (i, i + 1, i + 2), (1, -3, 2),
                      FAMILY_R12, signal_traded=True, carry_traded=False,
                      note="carry-neutral; cleanest signal read"))
    for i in range(1, max_generic - 1):
        add(Structure(f"RATIO_23_G{i}G{i+1}G{i+2}", (i, i + 1, i + 2), (2, -5, 3),
                      FAMILY_R23, signal_traded=True, carry_traded=False,
                      note="carry-neutral; largest deviations"))

    return s


STRUCTURES: dict[str, Structure] = _build()

SIGNAL_SET = [n for n, st in STRUCTURES.items() if st.signal_traded]
CARRY_SET = [n for n, st in STRUCTURES.items() if st.carry_traded]


def anchor_leg(st: Structure) -> int:
    """
    The generic whose calendar month defines the structure's seasonal identity:
    the middle leg for a three-leg structure, the front leg for a spread.
    """
    return st.legs[1] if st.n_legs == 3 else st.legs[0]
