"""Wire encoding for every value the risk engine persists.

The persisted representation of a number is specified HERE rather than
inherited from the host language's default formatting, because the store is a
cross-process, cross-restart and potentially cross-implementation interface: a
risk engine written in another language must read back exactly what this one
wrote, and vice versa.

Two rules make that true.

**Encoding — 17 significant digits.** ``%.17g`` is the shortest fixed
precision guaranteed to round-trip any IEEE-754 binary64 value, and every
language with a C-style ``printf`` produces byte-identical output for it.
Language-default formatting does not: Python's ``repr``, Java's
``Double.toString`` and C++'s ``operator<<`` all disagree on how many digits
to emit, so a counter written by one and read by another would drift.

**Decoding — one explicit grammar.** Host float parsers are far more
permissive than this format, and they disagree with each other about what they
accept. ``float()`` in Python takes ``"nan"``, ``"infinity"``, ``"1_0"`` and
surrounding whitespace; C's ``strtod`` additionally takes hex-float literals
such as ``"0x1p3"``; several accept non-ASCII digits. A risk counter that
parses as infinity in one implementation and fails in another is a real
divergence, so the grammar below is enforced explicitly and anything outside
it is rejected as unset rather than guessed at:

.. code-block:: text

    number := [ '+' | '-' ] ( digits [ '.' [ digits ] ] | '.' digits )
              [ ( 'e' | 'E' ) [ '+' | '-' ] digits ]
    digits := '0'..'9' { '0'..'9' }

ASCII digits only; no leading or trailing whitespace; no underscores; no
``nan``/``inf``; no hex floats. The decoded value must additionally be finite.

Booleans encode as ``"1"`` / ``"0"``. Anything else decodes to ``False`` —
a corrupt flag must never read as "risk control disabled".
"""

from __future__ import annotations

import math
import re

#: The persisted-number grammar, as a regular expression over ASCII.
#:
#: Two deliberate choices, both of which a port must reproduce:
#:
#: ``[0-9]`` rather than ``\d`` — ``\d`` matches non-ASCII digits in Python,
#: .NET and Unicode-mode Java, so ``"１２"`` would parse in some ports and not
#: others.
#:
#: ``\A``/``\Z`` rather than ``^``/``$`` — ``$`` also matches immediately
#: before a trailing newline, so ``"1.0\n"`` would slip through. Most regex
#: flavours share that behaviour; the end-of-string anchor is ``\Z`` here and
#: ``\z`` in Java, .NET, PCRE, Go and Rust.
_NUMBER_RE = re.compile(r"\A[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")

#: Digits emitted for a persisted float. 17 significant digits is the smallest
#: precision that round-trips every binary64 value exactly.
FLOAT_PRECISION = 17

TRUE = "1"
FALSE = "0"


def encode_float(value: float) -> str:
    """Encode a finite float for persistence.

    Non-finite input is a programming error upstream, not a storable state, so
    it encodes as ``"0"`` — the fail-safe value. (A persisted ``inf`` daily
    loss would read back as "infinite headroom".)
    """
    if not math.isfinite(value):
        return "0"
    return f"{value:.{FLOAT_PRECISION}g}"


def decode_float(raw: str | None) -> float | None:
    """Decode a persisted number, or ``None`` when absent, blank or malformed.

    ``None`` means "no usable value" for every failure mode — absent key, the
    empty-string clear encoding, a value outside the grammar, and a value that
    parses but is not finite. Callers decide what a missing value means; none
    of them may treat it as a permissive default.
    """
    if raw is None:
        return None
    if _NUMBER_RE.match(raw) is None:
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


def decode_positive_float(raw: str | None) -> float | None:
    """Decode a persisted number that is only meaningful when strictly positive.

    Used for the operator limit overrides, where zero and negative values are
    the documented "clear this override" encoding alongside the empty string.
    """
    value = decode_float(raw)
    if value is None or value <= 0:
        return None
    return value


def encode_bool(value: bool) -> str:
    return TRUE if value else FALSE


def decode_bool(raw: str | None) -> bool:
    """Decode a persisted flag. Anything but exactly ``"1"`` is ``False``.

    Deliberately strict and one-directional: every flag in this library
    enables a control or relaxes one, and the fail-safe reading of a damaged
    value is always the more conservative one.
    """
    return raw == TRUE
