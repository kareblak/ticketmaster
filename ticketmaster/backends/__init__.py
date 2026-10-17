"""Session backends, in the order auto-detect tries them. Adding one is a module with `NAME`,
`unavailable(env) -> str | None` (why it cannot run here) and `make(cwd, config, env) -> Backend`, plus its entry in
`BACKENDS`.

The backend in use is the one named by `--backend`, else `TICKETMASTER_BACKEND`, else `backend` in
`.ticketmaster.toml`, else the first available. A named one that is unknown or unavailable is an error, never a
fallback.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from . import herdr, plain
from .base import Backend, BackendError, CleanupView, Occupant, Released, SessionsUnreadable

__all__ = ["BACKENDS", "Backend", "BackendError", "ChoiceError", "CleanupView", "Occupant", "Released",
           "SessionsUnreadable", "choose", "resolve"]

ENV = "TICKETMASTER_BACKEND"
BACKENDS = (herdr, plain)


class ChoiceError(ValueError):
    """No backend to use: an unknown or unavailable named one, or none available."""


def choose(flag: str | None, env: Mapping[str, str], config: dict):
    """The backend module to use: flag, then env, then toml, then the first available."""
    name = flag or env.get(ENV) or config.get("backend")
    if name:
        module = next((b for b in BACKENDS if b.NAME == name), None)
        if module is None:
            known = ", ".join(b.NAME for b in BACKENDS)
            raise ChoiceError(f"unknown backend {name!r}; known backends: {known}")
        if reason := module.unavailable(env):
            raise ChoiceError(f"backend {name} unavailable: {reason}")
        return module
    reasons = []
    for module in BACKENDS:
        if (reason := module.unavailable(env)) is None:
            return module
        reasons.append(f"{module.NAME}: {reason}")
    raise ChoiceError(f"no session backend available ({'; '.join(reasons)})")


def resolve(cwd: Path, flag: str | None, config: dict, env: Mapping[str, str] | None = None) -> Backend:
    env = os.environ if env is None else env
    return choose(flag, env, config).make(cwd, config, env)
