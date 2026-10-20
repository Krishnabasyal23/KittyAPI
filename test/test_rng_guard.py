import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from randomness import LIVE_RNG_MODES
from round_manager import RoundManager


def test_live_modes_exclude_seeded_rng():
    assert "classical" not in LIVE_RNG_MODES
    assert "quantum" not in LIVE_RNG_MODES


@pytest.mark.parametrize("mode", ["classical", "quantum", "bogus"])
def test_round_manager_refuses_non_live_modes(mode):
    manager = RoundManager()
    session_id = manager.create_session()["session_id"]

    with pytest.raises(ValueError, match="not allowed for live rounds"):
        manager.start_round(session_id, Decimal("10.00"), rng_mode=mode)

    # nothing was charged
    assert manager.get_balance(session_id)["balance"] == "50"


def test_server_refuses_to_start_in_classical_mode():
    env = {**os.environ, "GAME_RNG_MODE": "classical"}
    result = subprocess.run(
        [sys.executable, "-c", "import main"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
    assert "Gameplay RNG must be" in result.stderr
