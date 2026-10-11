"""
Tests for scripts/bootstrap.py — filling a .env without ever overwriting a key the user set,
writing it owner-only, and reporting the wallets the app really uses.

"What the app really uses" is checked with the app's own `Settings` (pydantic-settings)
reading the written file, so bootstrap and the app can never disagree about a key.
No network needed.
"""

from __future__ import annotations

import stat

import pytest

from app.config import Settings
from scripts.bootstrap import (
    PAYOUT_ADDRESS_SETTINGS,
    PRIVATE_FILE_MODE,
    PRIVATE_KEY_SETTINGS,
    addresses_in_use,
    env_assignments,
    fill_missing_keys,
    overridden_settings,
    write_private_file,
)

USER_KEY = "0x" + "11" * 32
ALL_SETTINGS = PRIVATE_KEY_SETTINGS + PAYOUT_ADDRESS_SETTINGS


def settings_from(path) -> Settings:
    """The app's Settings reading ONLY this file."""
    return Settings(_env_file=path)


@pytest.mark.parametrize(
    "user_line",
    [
        f"RELAYER_PRIVATE_KEY= {USER_KEY}",
        f"RELAYER_PRIVATE_KEY = {USER_KEY}",
        f"export RELAYER_PRIVATE_KEY={USER_KEY}",
        f"relayer_private_key={USER_KEY}",
        f"RELAYER_PRIVATE_KEY='{USER_KEY}'",
    ],
)
def test_user_key_in_any_dotenv_form_is_kept_and_used_by_the_app(tmp_path, user_line):
    # Arrange
    env_file = tmp_path / ".env"
    text = f"{user_line}\nSELLER_SETTLER_PRIVATE_KEY=\n"

    # Act
    new_text, created, _ = fill_missing_keys(text)
    env_file.write_text(new_text)

    # Assert
    assert "RELAYER_PRIVATE_KEY" not in created
    assert user_line in new_text
    assert settings_from(env_file).relayer_private_key == USER_KEY


def test_empty_example_lines_are_filled_in_place(tmp_path):
    # Arrange
    env_file = tmp_path / ".env"
    text = "# wallets\n" + "".join(f"{name}=\n" for name in ALL_SETTINGS)

    # Act
    new_text, created, _ = fill_missing_keys(text)
    env_file.write_text(new_text)

    # Assert: every value generated, each name assigned exactly once, and readable by the app.
    assert created == ALL_SETTINGS
    assert all(len(values) == 1 and values[0] for values in env_assignments(new_text).values())
    assert all(addresses_in_use(settings_from(env_file)).values())


def test_later_user_value_after_an_empty_example_line_is_kept(tmp_path):
    # Arrange: dotenv uses the LAST assignment.
    text = f"RELAYER_PRIVATE_KEY=\nRELAYER_PRIVATE_KEY={USER_KEY}\n"

    # Act
    new_text, created, _ = fill_missing_keys(text)

    # Assert
    assert "RELAYER_PRIVATE_KEY" not in created
    assert env_assignments(new_text)["RELAYER_PRIVATE_KEY"][-1] == USER_KEY


def test_earlier_user_value_hidden_by_a_later_empty_line_is_never_overwritten():
    # Arrange
    text = f"RELAYER_PRIVATE_KEY={USER_KEY}\nRELAYER_PRIVATE_KEY=\n"

    # Act
    new_text, created, warnings = fill_missing_keys(text)

    # Assert: the user's key is still in the file, nothing generated for it, and the user is told.
    assert USER_KEY in new_text
    assert "RELAYER_PRIVATE_KEY" not in created
    assert any("RELAYER_PRIVATE_KEY" in warning for warning in warnings)


def test_env_file_is_written_owner_read_write_only(tmp_path):
    # Arrange
    env_file = tmp_path / ".env"
    env_file.write_text("OLD=1\n")
    env_file.chmod(0o644)

    # Act
    write_private_file(env_file, "NEW=1\n")

    # Assert
    assert stat.S_IMODE(env_file.stat().st_mode) == PRIVATE_FILE_MODE
    assert env_file.read_text() == "NEW=1\n"


def test_value_overridden_outside_the_repo_root_env_is_reported(tmp_path, monkeypatch):
    # Arrange: the repo-root .env says one key, an environment variable says another.
    env_file = tmp_path / ".env"
    env_file.write_text(f"RELAYER_PRIVATE_KEY={USER_KEY}\n")
    monkeypatch.setenv("RELAYER_PRIVATE_KEY", "0x" + "22" * 32)

    # Act
    overridden = overridden_settings(env_file.read_text(), settings_from(env_file))

    # Assert
    assert "RELAYER_PRIVATE_KEY" in overridden
