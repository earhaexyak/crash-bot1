"""
game_logic.py - Crash game math.
Virtual currency only. No real payments, no blockchain calls.
"""

import hashlib
import hmac
import secrets
import time

HOUSE_EDGE = 0.03          # 3% house edge (industry-standard range 1-5%)
GROWTH_RATE = 0.00006       # multiplier growth speed, tuned for ~0.05s tick updates
MAX_MULTIPLIER_CAP = 1000.0


def generate_server_seed() -> str:
    """Fresh server seed per round. Combined with a round nonce, this makes the
    crash point reproducible/auditable after the fact (provably-fair pattern)
    without needing a real blockchain oracle."""
    return secrets.token_hex(32)


def crash_point_from_seed(server_seed: str, round_id: int) -> float:
    """
    Deterministic crash point derived from HMAC(server_seed, round_id).
    Same formula used by most public crash-game implementations:
        h  = HMAC_SHA256(server_seed, round_id)
        r  = first 52 bits of h, normalized to [0, 1)
        crash = floor( (100 - house_edge*100) / (1 - r) ) / 100
    Floors at 1.00x. This can be published/audited after the round ends by
    revealing server_seed, so players can verify they weren't cheated.
    """
    h = hmac.new(server_seed.encode(), str(round_id).encode(), hashlib.sha256).hexdigest()
    r_int = int(h[:13], 16)               # 52 bits
    r = r_int / float(1 << 52)
    r = min(max(r, 1e-9), 1 - 1e-9)       # avoid div by zero / infinite crash

    edge_factor = 1 - HOUSE_EDGE
    raw = edge_factor / (1 - r)
    crash = max(1.00, round(raw, 2))
    return min(crash, MAX_MULTIPLIER_CAP)


def multiplier_at(elapsed_seconds: float) -> float:
    """Exponential growth curve, matches the classic crash-game visual feel."""
    import math
    return round(math.exp(GROWTH_RATE * elapsed_seconds * 1000), 2)


def elapsed_to_reach(multiplier: float) -> float:
    """Inverse of multiplier_at - used to know when to stop a round's timer."""
    import math
    return math.log(multiplier) / (GROWTH_RATE * 1000)
