"""
Provably-fair commit / reveal for Catino (restored from "Quantum box: provably fair seal", a9ac9d9).

When a round starts, the server publishes only a COMMITMENT:
    commitment = sha256(f"{round_id}:{value}:{salt}")
where `value` is the random integer the crash point is computed from (in hybrid mode it is the quantum
measurement XOR a classical secure random mask) and `salt` is a secret random string.

Once the cat has actually been distracted (crash time passed), /api/rounds/{id}/result and
/api/rounds/{id}/verify REVEAL value, salt and the raw ingredients. Anyone can check:
  1. sha256(f"{round_id}:{value}:{salt}") == commitment       -> the outcome was fixed before the round
  2. u = (value + 0.5) / 2**bits; crash = clamp((1 - house_edge) / u, 1, max_multiplier)
                                                              -> the crash point really comes from that value
  3. (hybrid) value == concat(quantum_samples) XOR classical_mask -> the qubits really fed the result

Seasons never touch this: Summer Zoomies uses its own draw, season events use their own RNG, and the
butterfly / cocoa bonus only changes the cash-out multiplier, never the sealed crash point.

The JSON field names match QuantumProof in the Unity client (Assets/KittyCrash/Scripts/Core/Fairness.cs).
"""

import hashlib
import json
import secrets

from crash_engine import HOUSE_EDGE, MAX_MULTIPLIER


class NotRevealedYet(Exception):
    """The round is still running (or the cat is still walking after a cash-out)."""


def commitment_for(round_id: str, value, salt: str) -> str:
    return hashlib.sha256(f"{round_id}:{value}:{salt}".encode("utf-8")).hexdigest()


def make_proof(round_id: str, draw: dict | None, crash_point: float) -> dict:
    salt = secrets.token_hex(16)

    if draw is not None and draw.get("integer") is not None:
        value = int(draw["integer"])
        scheme = "value"
    else:
        # The crash point did not come from generate_random (e.g. a test stub):
        # still commit to it, the client can check the hash but not the derivation.
        value = repr(float(crash_point))
        scheme = "crash"

    return {
        "scheme": scheme,
        "commitment": commitment_for(round_id, value, salt),
        "salt": salt,
        "value": value,
        "bits": draw.get("bits") if draw else None,
        "qubits": draw.get("qubits") if draw else None,
        "source": draw.get("mode") if draw else "unknown",
        "quantum_samples": draw.get("quantum_samples") if draw else None,
        "classical_mask": draw.get("classical_mask") if draw else None,
        "house_edge": HOUSE_EDGE,
        "max_multiplier": MAX_MULTIPLIER,
    }


def public_reveal(proof: dict | None, crash_point: float) -> dict | None:
    """What /result and /verify show once the round is over."""
    if not proof:
        return None
    out = dict(proof)
    out["value"] = str(proof["value"])   # big integers as strings for every JSON client
    if proof.get("classical_mask") is not None:
        out["classical_mask"] = str(proof["classical_mask"])
    out["crash_point_exact"] = crash_point
    return out


# Persistence (migration 0003): one column per proof field, so /verify works after a restart.

PROOF_COLUMNS = {
    "scheme": "proof_scheme",
    "commitment": "proof_commitment",
    "salt": "proof_salt",
    "value": "proof_value",
    "bits": "proof_bits",
    "qubits": "proof_qubits",
    "source": "proof_source",
    "quantum_samples": "proof_quantum_samples",
    "classical_mask": "proof_classical_mask",
    "house_edge": "proof_house_edge",
    "max_multiplier": "proof_max_multiplier",
}


def proof_to_columns(proof: dict | None) -> dict:
    if not proof:
        return {}
    cols = {column: proof.get(field) for field, column in PROOF_COLUMNS.items()}
    cols["proof_value"] = str(proof["value"])   # 36-bit integers and repr() floats both as text
    if proof.get("quantum_samples") is not None:
        cols["proof_quantum_samples"] = json.dumps(list(proof["quantum_samples"]))
    if proof.get("classical_mask") is not None:
        cols["proof_classical_mask"] = str(proof["classical_mask"])
    return cols


def proof_from_columns(row: dict) -> dict | None:
    """Rebuild exactly what make_proof() returned, from a rounds row."""
    if not row.get("proof_commitment"):
        return None
    proof = {field: row.get(column) for field, column in PROOF_COLUMNS.items()}
    if proof["scheme"] == "value":
        proof["value"] = int(proof["value"])
    if proof["quantum_samples"] is not None:
        proof["quantum_samples"] = json.loads(proof["quantum_samples"])
    if proof["classical_mask"] is not None:
        proof["classical_mask"] = int(proof["classical_mask"])
    return proof


def verify(round_id: str, reveal: dict, crash_point: float, published_commitment: str | None = None) -> dict:
    """Server-side mirror of what the game client checks (Fairness.Verify in Unity).

    published_commitment: the hash shown at round start. The reveal must match it, not just itself.
    """
    value = reveal["value"]
    ok_hash = commitment_for(round_id, value, reveal["salt"]) == reveal["commitment"]
    if published_commitment is not None:
        ok_hash = ok_hash and reveal["commitment"] == published_commitment
    ok_math = None
    ok_quantum = None
    if reveal.get("scheme") == "value":
        v = int(value)
        u = (v + 0.5) / (2 ** reveal["bits"])
        crash = max(1.0, min((1.0 - reveal["house_edge"]) / u, reveal["max_multiplier"]))
        ok_math = abs(crash - crash_point) < 1e-9
        samples = reveal.get("quantum_samples")
        if samples:
            combined = 0
            for s in samples:
                combined = (combined << reveal["qubits"]) | int(s)
            mask = int(reveal["classical_mask"]) if reveal.get("classical_mask") is not None else 0
            ok_quantum = (combined ^ mask) == v
    return {"hash": ok_hash, "math": ok_math, "quantum": ok_quantum}
