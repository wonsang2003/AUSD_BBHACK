"""
Read and update the repo-root `.env` safely (used by deploy.py and other scripts).

Rules:
  * Parsing uses python-dotenv, the same parser the app's settings use (last assignment wins).
  * A NAME that already has a non-empty value is never overwritten unless `overwrite=True`.
  * The file is rewritten atomically with mode 0600 (it holds private keys).
  * Nothing here prints values: callers print only addresses, never secrets.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from dotenv.parser import parse_stream

from app.config import REPO_ROOT
from scripts.bootstrap import env_assignments, write_private_file

ENV_PATH = REPO_ROOT / ".env"


@dataclass
class EnvUpdate:
    """Which names were written and which were kept because they already had a value."""

    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    """NAME (upper-cased) → its effective (last) value; {} if the file does not exist."""
    if not path.exists():
        return {}
    return {name: values[-1] for name, values in env_assignments(path.read_text()).items()}


def replace_assignment(text: str, name: str, value: str, *, overwrite: bool) -> str:
    """Write NAME=value over every assignment of NAME (only the empty ones unless `overwrite`), or append it."""
    lines, replaced = [], False
    for binding in parse_stream(io.StringIO(text)):
        is_target = binding.key is not None and binding.key.upper() == name
        if is_target and (overwrite or not (binding.value or "").strip()):
            lines.append(f"{name}={value}\n")
            replaced = True
        else:
            lines.append(binding.original.string)
    new_text = "".join(lines)
    if not replaced:
        separator = "" if new_text.endswith("\n") or not new_text else "\n"
        new_text = f"{new_text}{separator}{name}={value}\n"
    return new_text


def set_env_values(values: dict[str, str], *, overwrite: bool = False, path: Path = ENV_PATH) -> EnvUpdate:
    """Set several NAME=value pairs. Rule: a non-empty existing value is kept unless `overwrite`."""
    text = path.read_text() if path.exists() else ""
    current = {name: vals[-1] for name, vals in env_assignments(text).items()}
    update = EnvUpdate()
    for name, value in values.items():
        if current.get(name) and not overwrite:
            update.kept.append(name)
            continue
        text = replace_assignment(text, name, value, overwrite=overwrite)
        update.written.append(name)
    if update.written:
        write_private_file(path, text)
    return update
