"""
Parity tests for the no-PyYAML fallback config parser.

The fallback only earns its place if it agrees with ``yaml.safe_load``
on the constructs pokebot-3ds configs actually use. Every case here is
asserted against PyYAML itself rather than against a hand-written
expectation, so the tests stay honest if the config grows new shapes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokebot.config_io import load_config, parse_simple_yaml  # noqa: E402

yaml = pytest.importorskip("yaml", reason="parity is measured against PyYAML")

REPO_ROOT = Path(__file__).resolve().parents[1]


CASES = {
    "scalars": """
mode: observe
count: 12
hexval: 0x08CE1CF8
ratio: 0.20
enabled: true
disabled: false
nothing: null
tilde: ~
""",
    "inline_list": """
random_encounters:
  run_local: [0.50, 0.86]
  run_touch: null
  empty_list: []
""",
    "nested_maps": """
input:
  dry_run: false
  binds:
    A: a
    Select: n
    DpadUp: t
rpc:
  host: 127.0.0.1
  port: 45987
""",
    "block_list": """
targets:
  mode: all
  rules:
    - shiny: true
    - perfect_iv_count_min: 5
""",
    "comments_everywhere": """
# leading comment
mode: observe        # trailing comment
offsets:
  # comment inside a block
  party_base: 0x08CE1CF8   # another trailing one
  party_stride: 484
""",
    "quoted_strings": """
trainer_name: 'Roman'
note: "a # inside quotes stays"
empty_string: ''
""",
    "deep_nesting": """
a:
  b:
    c:
      d: 1
""",
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_matches_pyyaml(name: str) -> None:
    text = CASES[name]
    assert parse_simple_yaml(text) == yaml.safe_load(text)


def test_shipped_config_matches_pyyaml() -> None:
    """The config that actually ships must round-trip identically."""
    text = (REPO_ROOT / "config.yaml").read_text(encoding="utf-8")
    assert parse_simple_yaml(text) == yaml.safe_load(text)


def test_inline_list_is_a_list_not_a_string() -> None:
    """Regression: run_local[0] used to be the character '['.

    encounter._run_fraction does float(run_local[0]), which raised
    ValueError and killed the hunt on the first wild battle.
    """
    cfg = parse_simple_yaml("random_encounters:\n  run_local: [0.50, 0.86]\n")
    run_local = cfg["random_encounters"]["run_local"]
    assert run_local == [0.5, 0.86]
    assert float(run_local[0]) == 0.5


def test_null_is_none_not_a_truthy_string() -> None:
    """Regression: run_touch: null used to be the truthy string 'null',
    so the auto-positioned RUN touch point was overridden by garbage."""
    cfg = parse_simple_yaml("random_encounters:\n  run_touch: null\n")
    assert cfg["random_encounters"]["run_touch"] is None
    assert not cfg["random_encounters"]["run_touch"]


def test_single_letter_keybinds_stay_strings() -> None:
    """YAML 1.1 makes yes/no/on/off booleans but NOT single letters —
    'n' is the Select bind, and turning it into False breaks input."""
    cfg = parse_simple_yaml("input:\n  binds:\n    Select: n\n    A: y\n")
    assert cfg["input"]["binds"] == {"Select": "n", "A": "y"}


def test_hex_offsets_parse_as_ints() -> None:
    cfg = parse_simple_yaml("offsets:\n  party_base: 0x08CE1CF8\n")
    assert cfg["offsets"]["party_base"] == 0x08CE1CF8


def test_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    assert load_config(tmp_path / "nope.yaml") == {}


def test_json_config_loads(tmp_path: Path) -> None:
    p = tmp_path / "cfg.json"
    p.write_text('{"mode": "observe", "offsets": {"party_base": 1}}')
    assert load_config(p) == {"mode": "observe", "offsets": {"party_base": 1}}


def test_malformed_config_raises_rather_than_running_on_defaults(
    tmp_path: Path,
) -> None:
    """A silently-empty config sends the bot at the wrong RAM region."""
    p = tmp_path / "cfg.json"
    p.write_text("{not valid json")
    with pytest.raises(Exception):
        load_config(p)
