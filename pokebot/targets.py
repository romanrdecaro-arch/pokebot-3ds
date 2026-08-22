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


#: Rule fields that accept a list of allowed values. YAML users
#: naturally write ``species: 25`` rather than ``species: [25]``, and
#: a bare string like ``nature: Adamant`` must not be treated as an
#: iterable of characters.
_LIST_FIELDS = ("species", "nature", "gender", "ability_num", "held_item")

#: Rule fields that map a stat name to a number.
_IV_DICT_FIELDS = ("iv_min", "iv_max", "iv_exact")

#: The canonical PK6 stat keys, as produced by ``parser.parse_pkm``.
_STAT_NAMES = ("HP", "Atk", "Def", "Spe", "SpA", "SpD")


def _as_list(value) -> list:
    """Wrap a lone scalar so ``set()`` over it means what the user meant."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return [value]
    return list(value)


def _check_iv_dict(field_name: str, value) -> dict:
    if not isinstance(value, dict):
        raise ValueError(
            f"target rule '{field_name}' must be a mapping of stat to "
            f"number, e.g. {{HP: 31, Atk: 31}} — got {value!r}"
        )
    unknown = [k for k in value if k not in _STAT_NAMES]
    if unknown:
        raise ValueError(
            f"target rule '{field_name}' has unknown stat name(s) "
            f"{unknown}; valid names are: {', '.join(_STAT_NAMES)}"
        )
    return dict(value)


def target_from_dict(d: dict) -> Target:
    """Build a Target from a dict (typically loaded from YAML).

    Validates strictly, and deliberately so. Every mistake this catches
    used to fail *silently at hunt time*: a typo'd key like ``shinny``
    produced a rule with no constraints, which matches every Pokemon
    and halts the hunt on the first encounter; a scalar ``species: 25``
    raised deep inside ``Rule.matches`` and swallowed the shiny save;
    and ``nature: Adamant`` became a set of seven characters that
    nothing ever matched. Failing loudly at startup is the whole point.
    """
    if not d:
        return Target(mode="all", rules=[])

    mode = d.get("mode", "all")
    if mode not in ("all", "any"):
        raise ValueError(
            f"target 'mode' must be 'all' or 'any' — got {mode!r}")

    rules_in = d.get("rules", [])
    if isinstance(rules_in, dict):        # tolerate a single rule
        rules_in = [rules_in]

    rules: list[Rule] = []
    for i, r in enumerate(rules_in):
        if not isinstance(r, dict):
            raise ValueError(
                f"target rule #{i + 1} must be a mapping — got {r!r}")
        unknown = [k for k in r if k not in Rule.__dataclass_fields__]
        if unknown:
            raise ValueError(
                f"target rule #{i + 1} has unknown key(s) {unknown}; "
                f"valid keys are: "
                f"{', '.join(sorted(Rule.__dataclass_fields__))}"
            )
        kwargs = {}
        for key, value in r.items():
            if value is None:
                continue
            if key in _LIST_FIELDS:
                kwargs[key] = _as_list(value)
            elif key in _IV_DICT_FIELDS:
                kwargs[key] = _check_iv_dict(key, value)
            else:
                kwargs[key] = value
        rules.append(Rule(**kwargs))
    return Target(mode=mode, rules=rules)
