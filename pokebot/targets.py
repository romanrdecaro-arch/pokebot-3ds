"""
Target / filter system.

A Target describes the criteria a Pokémon must meet to count as a "hit"
for the bot. Multiple criteria can be combined with all-of (AND) or
any-of (OR) semantics.

Examples in YAML config:

  target:
    mode: all
    rules:
      - shiny: true
      - iv_sum_min: 150

  target:
    mode: any
    rules:
      - shiny: true
      - nature: [Adamant, Jolly]
        iv_min:
          HP: 25
          Atk: 31
          Spe: 31
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .parser import ParsedPokemon


@dataclass
class Rule:
    """A single criterion. All non-None fields must be satisfied."""
    shiny: Optional[bool] = None
    species: Optional[Iterable[int]] = None       # list of dex IDs allowed
    nature: Optional[Iterable[str]] = None        # list of nature names
    gender: Optional[Iterable[str]] = None        # subset of {"M","F","G"}
    ability_num: Optional[Iterable[int]] = None
    held_item: Optional[Iterable[int]] = None
    iv_min: Optional[dict] = None                 # {"HP":31,"Atk":31,...}
    iv_max: Optional[dict] = None
    iv_exact: Optional[dict] = None
    iv_sum_min: Optional[int] = None
    iv_sum_max: Optional[int] = None
    perfect_iv_count_min: Optional[int] = None    # how many IVs must be 31
    fateful_encounter: Optional[bool] = None

    def matches(self, p: ParsedPokemon) -> bool:
        if self.shiny is not None and p.shiny != self.shiny:
            return False
        if self.species and p.species not in set(self.species):
            return False
        if self.nature and p.nature not in set(self.nature):
            return False
        if self.gender and p.gender not in set(self.gender):
            return False
        if self.ability_num and p.ability_num not in set(self.ability_num):
            return False
        if self.held_item and p.held_item not in set(self.held_item):
            return False
        if self.fateful_encounter is not None \
                and p.fateful_encounter != self.fateful_encounter:
            return False

        if self.iv_min:
            for stat, mn in self.iv_min.items():
                if p.ivs.get(stat, 0) < mn:
                    return False
        if self.iv_max:
            for stat, mx in self.iv_max.items():
                if p.ivs.get(stat, 0) > mx:
                    return False
        if self.iv_exact:
            for stat, val in self.iv_exact.items():
                if p.ivs.get(stat, 0) != val:
                    return False

        if self.iv_sum_min is not None and sum(p.ivs.values()) < self.iv_sum_min:
            return False
        if self.iv_sum_max is not None and sum(p.ivs.values()) > self.iv_sum_max:
            return False

        if self.perfect_iv_count_min is not None:
            n = sum(1 for v in p.ivs.values() if v == 31)
            if n < self.perfect_iv_count_min:
                return False
        return True


@dataclass
class Target:
    """A whole target: collection of rules combined with all/any."""
    mode: str = "all"                  # "all" or "any"
    rules: list[Rule] = field(default_factory=list)

    def matches(self, p: ParsedPokemon) -> bool:
        if not self.rules:
            return False  # an empty target matches nothing
        if self.mode == "any":
            return any(r.matches(p) for r in self.rules)
        return all(r.matches(p) for r in self.rules)

    def describe(self, p: ParsedPokemon) -> str:
        """Concise reason string for logs / dashboard."""
        bits = []
        if p.shiny:
            bits.append("SHINY")
        bits.append(p.nature)
        bits.append(f"IVs {sum(p.ivs.values())}")
        n31 = sum(1 for v in p.ivs.values() if v == 31)
        if n31:
            bits.append(f"{n31}×31")
        return " | ".join(bits)


def target_from_dict(d: dict) -> Target:
    """Build a Target from a dict (typically loaded from YAML)."""
    if not d:
        return Target(mode="all", rules=[])
    rules_in = d.get("rules", [])
    rules: list[Rule] = []
    for r in rules_in:
        # tolerate single dict instead of list
        rules.append(Rule(**{k: v for k, v in r.items()
                             if k in Rule.__dataclass_fields__}))
    return Target(mode=d.get("mode", "all"), rules=rules)
