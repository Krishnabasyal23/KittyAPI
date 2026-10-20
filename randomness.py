import random
import secrets

from typing import Literal
from qrng import generate_quantum_number

RNGMode = Literal["classical", "secure", "quantum", "hybrid"]

# Only these may decide real rounds. "classical" is seeded (predictable) and "quantum" is a bare
# simulator draw, so both are for tests and offline experiments only.
LIVE_RNG_MODES = frozenset({"hybrid", "secure"})

prng = random.Random(2026)

def classical_rng(bits: int = 36) -> int:
    return prng.getrandbits(bits)

def secure_rng(bits: int = 36) -> int:
    return secrets.randbits(bits)

def quantum_samples(qubits: int = 6, samples: int = 6) -> list[int]:
    """One measured register per sample, validated."""

    numbers = generate_quantum_number(qubits=qubits, samples=samples)

    if len(numbers) != samples:
        raise ValueError("Incorrect quantum sample count")

    for number in numbers:
        if not isinstance(number, int):
            raise ValueError("Quantum sample must be an integer")

        if not 0 <= number < (1 << qubits):
            raise ValueError("Quantum sample outside valid range")

    return list(numbers)

def combine_samples(numbers: list[int], qubits: int) -> int:
    combined = 0

    for number in numbers:
        combined = (combined << qubits) | number

    return combined

def quantum_rng(qubits: int = 6, samples: int = 6) -> int:
    return combine_samples(quantum_samples(qubits, samples), qubits)

def hybrid_draw(qubits: int = 6, samples: int = 6) -> dict:
    """value = concat(quantum samples) XOR secure mask, with the ingredients kept for the fairness proof."""
    total_bits = qubits * samples
    try:
        measured = quantum_samples(qubits, samples)
        mask = secure_rng(total_bits)
        return {
            "value": combine_samples(measured, qubits) ^ mask,
            "source": "hybrid",
            "quantum_samples": measured,
            "classical_mask": mask,
        }

    except Exception:
        return {
            "value": secure_rng(total_bits),
            "source": "secure_fallback",
            "quantum_samples": None,
            "classical_mask": None,
        }

def hybrid_rng(qubits: int = 6, samples: int = 6) -> tuple[int, str]:
    draw = hybrid_draw(qubits, samples)
    return draw["value"], draw["source"]

def generate_random(mode: RNGMode = "hybrid", qubits: int = 6, samples: int = 6) -> dict:

    if not 1 <= qubits <= 10:
        raise ValueError("Qubits must be between 1 and 10")

    if not 1 <= samples <= 64:
        raise ValueError("Samples must be between 1 and 64")

    total_bits = qubits * samples
    measured = None
    mask = None

    if mode == "classical":
        value = classical_rng(total_bits)
        source = "classical"

    elif mode == "secure":
        value = secure_rng(total_bits)
        source = "secure"

    elif mode == "quantum":
        measured = quantum_samples(qubits, samples)
        value = combine_samples(measured, qubits)
        source = "quantum_simulated"

    elif mode == "hybrid":
        draw = hybrid_draw(qubits, samples)
        value, source = draw["value"], draw["source"]
        measured, mask = draw["quantum_samples"], draw["classical_mask"]

    else:
        raise ValueError(f"Unknown RNG mode: {mode}")

    normalized = (value + 0.5) / (2 ** total_bits)

    return {
        "mode": source,
        "bits": total_bits,
        "integer": value,
        "normalized": normalized,
        # ingredients for the fairness proof (None when the mode has none)
        "qubits": qubits,
        "quantum_samples": measured,
        "classical_mask": mask,
    }
