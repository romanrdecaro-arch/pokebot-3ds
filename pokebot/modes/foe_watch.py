"""
A "is there a new wild yet?" check fast enough to react to a bite.

The full foe-window scan is 128 RPC round trips over a 128 KB window
plus ~32,000 parse attempts. That is fine once per loop iteration, and
far too slow to poll with: Azahar runs this hunt around 600%, where
X/Y's bite window is roughly 170 ms of wall time. A check costing more
than that cannot land inside it, which is why fishing missed most
bites -- the A press was not late by a little, it was often looking at
a window that had already closed.

The game reuses the same buffer for the wild slot, so once the address
is known, re-reading that ONE record answers the question in a single
round trip. The full scan still runs periodically to re-acquire the
address if the slot moves, which it does between sessions.

Gen 7 writes its wild to one of two windows (USUM, 25,300 logged wilds:
0x3254F4AC in the main one 83% of the time, 0x3002F9A0 / 0x30039888 in
the extra one the rest), so with ``windows`` the full scan covers them
all, and the cheap check also reads every slot in ``slots`` -- the
game's known wild slots, from the first check on.
"""
from __future__ import annotations

import logging

from .observe import read_pk6_at, scan_nonparty

log = logging.getLogger(__name__)


class FoeWatch:
    """Watches the foe window for a record ``seen`` does not contain.

    ``seen`` is the live set the hunt maintains, so anything already
    reported is ignored and only a genuinely new wild counts.
    """

    def __init__(self, ctx, foe_base: int, foe_len: int,
                 party_keys: set, seen: set, full_every: int = 10,
                 windows=None):
        self.ctx = ctx
        self.foe_base = foe_base
        self.foe_len = foe_len
        #: Every (base, length) the full scan reads: the one window, or
        #: all a Gen 7 game declares (observe.foe_windows).
        self.windows = list(windows) if windows else [(foe_base, foe_len)]
        self.party_keys = party_keys
        self.seen = seen
        # How many cheap checks before paying for a full scan. The
        # cheap one only looks where the last wild was; this is what
        # finds it again if the slot moved.
        self.full_every = max(1, int(full_every))
        self.hot: int = 0
        #: Where the game is known to write its wild (GameOffsets.
        #: wild_slots), read by every cheap check, ``hot`` or not. Fixed:
        #: a slot learned and later dropped is how one went unwatched.
        self.slots: list = []
        self._since_full = 0
        self.fast_checks = 0
        self.full_scans = 0

    def _is_new(self, pkm) -> bool:
        # A wild has no OT until it is caught. Gen 7 keeps the player's
        # battle copies in the same window, so an owned record must
        # never count even if the party read missed it.
        return bool(pkm is not None
                    and not (getattr(pkm, "ot_name", "") or "")
                    and pkm.encryption_key not in self.party_keys
                    and pkm.encryption_key not in self.seen)

    def _fast(self) -> bool:
        """One read at the last known wild address, and one at each known
        wild slot."""
        addrs = [a for a in dict.fromkeys([self.hot] + self.slots) if a]
        if not addrs:
            return False
        self.fast_checks += 1
        for addr in addrs:
            if self._is_new(read_pk6_at(self.ctx, addr)):
                self.hot = addr
                return True
        return False

    def _full(self) -> bool:
        """The whole window. Also re-learns the hot address."""
        self.full_scans += 1
        self._since_full = 0
        keys, cands = set(), []
        for base, length in self.windows:
            for addr, pkm in scan_nonparty(self.ctx, base, length,
                                           self.party_keys):
                if pkm.encryption_key not in keys:
                    keys.add(pkm.encryption_key)
                    cands.append((addr, pkm))
        cands.sort(key=lambda ap: ap[0])
        newest = None
        for addr, pkm in cands:
            # Remember where records live even when nothing is new, so
            # the cheap path has somewhere to look next time.
            self.hot = self.hot or addr
            # scan_nonparty already drops party members, but the test
            # for "is this new" lives in _is_new and both paths must
            # answer the same way -- a check that disagrees with
            # itself depending on which branch ran is a bug waiting.
            if self._is_new(pkm):
                newest = (addr, pkm)
        if newest is None:
            return False
        self.hot = newest[0]
        return True

    def check(self, full: bool = False) -> bool:
        """True the moment a wild the hunt has not seen is present.
        ``full``: sweep every window now, not at the next full_every."""
        if self._fast():
            return True
        self._since_full += 1
        if (not full and (self.hot or self.slots)
                and self._since_full < self.full_every):
            return False
        return self._full()

    def stats(self) -> str:
        return (f"{self.fast_checks} fast check(s), "
                f"{self.full_scans} full scan(s)")
