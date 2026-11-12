"""Count agent starts against fixed limits, across outputs and retries."""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .persistence import digest, write_json

_OWNERS = {}
_OWNERS_LOCK = threading.Lock()


def _after_fork():
    """A forked child must take its own hold, not inherit its parent's."""
    global _OWNERS_LOCK
    for owner in _OWNERS.values():
        owner["lock"].close()
    _OWNERS.clear()
    _OWNERS_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class StartLimitError(RuntimeError):
    pass


class StartLedger:
    """A file that counts agent starts of each kind against fixed limits.

    A start is charged before the agent runs and is never refunded, including
    when the process dies before the agent begins. Several outputs and retries
    can share one ledger file, so a study cannot spend its budget twice.

    Limits count starts. They do not cap tokens or dollars; a start whose cost
    was not reported is recorded with an unknown cost.
    """

    def __init__(self, path: Path, *, limits: dict[str, int], identity: dict | str):
        if not limits or any(type(v) is not int or v < 0 for v in limits.values()):
            raise ValueError("Start limits must be nonnegative integers")
        self.path = Path(path).resolve()
        self.limits = limits
        if isinstance(identity, str):
            if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
                raise ValueError("An opaque allocation identity must be a SHA256 hex digest")
            self.identity_hash = identity
        else:
            self.identity_hash = digest(identity)
        with self._locked():
            if self.path.exists():
                self._read()
            else:
                write_json(
                    self.path,
                    {
                        "limits": limits,
                        "identity_hash": self.identity_hash,
                        "counts": dict.fromkeys(limits, 0),
                        "starts": {},
                    },
                )

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _read(self):
        data = json.loads(self.path.read_text())
        if data["limits"] != self.limits or data["identity_hash"] != self.identity_hash:
            raise ValueError("Shared budget setup changed")
        return data

    @contextmanager
    def allocation_owner(self):
        """Hold this ledger for one controller. Nested use in the same thread shares the hold."""
        with _OWNERS_LOCK:
            owner = _OWNERS.get(self.path)
            if owner is not None and owner["thread"] != threading.get_ident():
                raise RuntimeError("Another experiment controller owns this shared budget")
            if owner is None:
                lock = self.path.with_suffix(self.path.suffix + ".owner").open("a+b", buffering=0)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException as exc:
                    lock.close()
                    if isinstance(exc, BlockingIOError):
                        raise RuntimeError("Another experiment controller owns this shared budget") from exc
                    raise
                owner = {"thread": threading.get_ident(), "depth": 0, "lock": lock}
                _OWNERS[self.path] = owner
            owner["depth"] += 1
        try:
            yield
        finally:
            with _OWNERS_LOCK:
                if _OWNERS.get(self.path) is owner:
                    owner["depth"] -= 1
                    if owner["depth"] == 0:
                        owner["lock"].close()
                        del _OWNERS[self.path]

    def require_capacity(self, requests: dict[str, int]):
        """Raise unless every requested start still fits. Call while holding allocation_owner()."""
        with self._locked():
            data = self._read()
            for kind, amount in requests.items():
                if type(amount) is not int or amount < 0 or kind not in self.limits:
                    raise ValueError("Invalid panel capacity request")
                if data["counts"][kind] + amount > self.limits[kind]:
                    raise StartLimitError(f"Requested evaluations exceed the {kind} start budget")

    def start(self, kind: str, label: str) -> str:
        with self._locked():
            data = self._read()
            if kind not in self.limits or data["counts"][kind] >= self.limits[kind]:
                raise StartLimitError(f"{kind} start budget exhausted")
            key = uuid4().hex
            data["counts"][kind] += 1
            data["starts"][key] = {
                "kind": kind,
                "label": label,
                "status": "started",
                "started_at": time.time(),
            }
            write_json(self.path, data)
            return key

    def finish(self, key: str, *, status: str, cost_usd=None):
        if cost_usd is not None and (
            type(cost_usd) not in (int, float) or not math.isfinite(cost_usd) or cost_usd < 0
        ):
            raise ValueError("Cost must be a finite nonnegative number or unknown")
        with self._locked():
            data = self._read()
            data["starts"][key].update(status=status, cost_usd=cost_usd, finished_at=time.time())
            write_json(self.path, data)

    def snapshot(self):
        with self._locked():
            return self._read()

    def remaining(self, kind: str) -> int:
        """Starts of one kind still available."""
        return self.limits[kind] - self.snapshot()["counts"][kind]
