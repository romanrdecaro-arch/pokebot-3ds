"""
Config loading for pokebot-3ds.

``run.py`` and ``launcher.py`` each used to carry their own copy of a
tiny YAML parser for users who never installed PyYAML. Both copies
mis-parsed two constructs that ship in the default ``config.yaml``:

* inline flow lists — ``run_local: [0.50, 0.86]`` came back as the
  *string* ``"[0.50, 0.86]"``, so ``run_local[0]`` was the character
  ``"["`` and fleeing a wild battle raised ``ValueError`` on the first
  encounter.
* ``null`` — came back as the *string* ``"null"``, which is truthy, so
  ``run_touch: null`` was treated as a manual touch-point override.

This module is the single implementation both entry points now use.
PyYAML is still preferred when importable; the fallback exists only so
a user with no network access still gets a working bot, and it is
tested for parity against PyYAML on the shipped config.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# PyYAML (YAML 1.1) resolves these full words to booleans. Single
# letters like the ``n`` in ``Select: n`` are NOT booleans — the key
# binds in config.yaml depend on that.
_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}
_NULL = {"", "null", "~"}

_INT_RE = re.compile(r"^[+-]?\d+$")
_HEX_RE = re.compile(r"^[+-]?0[xX][0-9a-fA-F]+$")
_OCT_RE = re.compile(r"^[+-]?0[oO][0-7]+$")


def _strip_comment(line: str) -> str:
    """Drop a trailing ``# comment``, ignoring ``#`` inside quotes."""
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _scalar(text: str) -> Any:
    """Coerce one YAML scalar the way ``yaml.safe_load`` would."""
    v = text.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    low = v.lower()
    if low in _NULL:
        return None
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if _HEX_RE.match(v):
        return int(v, 16)
    if _OCT_RE.match(v):
        return int(v, 8)
    if _INT_RE.match(v):
        return int(v)
    try:
        return float(v)
    except ValueError:
        return v


def _split_flow(body: str) -> list[str]:
    """Split ``a, b, [c, d]`` on top-level commas only."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for ch in body:
        if quote:
            if ch == quote:
                quote = None
            current.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail or parts:
        parts.append(tail)
    return [p.strip() for p in parts]


def _value(text: str) -> Any:
    """Coerce a scalar or an inline flow collection."""
    v = text.strip()
    if v.startswith("[") and v.endswith("]"):
        body = v[1:-1].strip()
        return [] if not body else [_value(p) for p in _split_flow(body)]
    if v.startswith("{") and v.endswith("}"):
        body = v[1:-1].strip()
        out: dict[str, Any] = {}
        for pair in _split_flow(body) if body else []:
            k, _, val = pair.partition(":")
            out[_scalar(k)] = _value(val)
        return out
    return _scalar(v)


def _tokenize(text: str) -> list[tuple[int, str]]:
    """Significant lines as ``(indent, content)``, comments removed."""
    tokens: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        tokens.append((len(line) - len(line.lstrip()), line.strip()))
    return tokens


def _parse_block(tokens: list[tuple[int, str]], pos: int,
                 indent: int) -> tuple[Any, int]:
    """Parse every token at ``indent`` into a dict or list."""
    if pos < len(tokens) and tokens[pos][1].startswith("- "):
        return _parse_list(tokens, pos, indent)
    if pos < len(tokens) and tokens[pos][1] == "-":
        return _parse_list(tokens, pos, indent)
    return _parse_map(tokens, pos, indent)


def _parse_map(tokens: list[tuple[int, str]], pos: int,
               indent: int) -> tuple[dict, int]:
    out: dict[str, Any] = {}
    while pos < len(tokens):
        line_indent, content = tokens[pos]
        if line_indent < indent:
            break
        if ":" not in content:
            pos += 1
            continue
        key, _, rest = content.partition(":")
        key = _scalar(key)
        rest = rest.strip()
        pos += 1
        if rest:
            out[key] = _value(rest)
            continue
        # Empty value: either a nested block or a genuine null.
        if pos < len(tokens) and tokens[pos][0] > line_indent:
            out[key], pos = _parse_block(tokens, pos, tokens[pos][0])
        elif (pos < len(tokens) and tokens[pos][0] == line_indent
                and tokens[pos][1].startswith("-")):
            out[key], pos = _parse_list(tokens, pos, line_indent)
        else:
            out[key] = None
    return out, pos


def _parse_list(tokens: list[tuple[int, str]], pos: int,
                indent: int) -> tuple[list, int]:
    out: list[Any] = []
    while pos < len(tokens):
        line_indent, content = tokens[pos]
        if line_indent < indent or not content.startswith("-"):
            break
        item = content[1:].strip()
        pos += 1
        if not item:
            if pos < len(tokens) and tokens[pos][0] > line_indent:
                value, pos = _parse_block(tokens, pos, tokens[pos][0])
            else:
                value = None
            out.append(value)
            continue
        if ":" in item and not item.startswith(("[", "{", "'", '"')):
            # "- key: value" starts a mapping owned by this item; any
            # deeper lines that follow belong to that same mapping.
            key, _, rest = item.partition(":")
            entry: dict[str, Any] = {
                _scalar(key): _value(rest) if rest.strip() else None
            }
            if (pos < len(tokens) and tokens[pos][0] > line_indent
                    and not tokens[pos][1].startswith("- ")):
                nested, pos = _parse_map(tokens, pos, tokens[pos][0])
                entry.update(nested)
            out.append(entry)
        else:
            out.append(_value(item))
    return out, pos


def parse_simple_yaml(text: str) -> dict:
    """Parse the YAML subset pokebot-3ds configs use, without PyYAML."""
    tokens = _tokenize(text)
    if not tokens:
        return {}
    value, _ = _parse_block(tokens, 0, tokens[0][0])
    return value if isinstance(value, dict) else {}


def load_config(path: str | Path) -> dict:
    """Load a ``.yaml``/``.yml``/``.json`` config into a dict.

    Returns ``{}`` for a missing file. Raises on a malformed one — a
    silently empty config means the bot runs with default offsets and
    hunts the wrong memory region, which is far worse than a crash at
    startup with a readable error.
    """
    cfg_path = Path(path)
    if not cfg_path.exists():
        return {}
    text = cfg_path.read_text(encoding="utf-8")
    if cfg_path.suffix.lower() == ".json":
        return json.loads(text) or {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return parse_simple_yaml(text)
    return yaml.safe_load(text) or {}
