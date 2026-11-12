"""A two-line terminal status display for a running experiment.

``Research`` prints nothing by default. Pass a ``TerminalProgress`` as its
``progress`` argument to see the display. It draws only on an interactive
terminal, so redirected output stays plain.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading


def _plain(value) -> str:
    """Drop control characters, so a name or path cannot alter the terminal or add lines."""
    return "".join(char for char in str(value) if char.isprintable())


class TerminalProgress:
    """Redraws the current candidate, phase, budget and latest metrics on stderr.

    Use it as a context manager so the last line is finished even after an error.
    Pass ``enabled=False`` when the output is read by a program or an agent.
    """

    def __init__(self, *, stream=None, enabled=True):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = bool(enabled and self.stream.isatty() and os.environ.get("TERM") != "dumb")
        self._lock = threading.Lock()
        self._sequence = -1
        self._drawn = False
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __call__(self, event):
        if not self.enabled:
            return
        with self._lock:
            sequence = event.get("sequence", 0)
            if self._closed or sequence <= self._sequence:
                return
            self._sequence = sequence
            candidate = event.get("candidate_number", "?")
            limit = event.get("candidate_limit", "?")
            phase = _plain(event.get("stage", "working")).replace("_", " ")
            if event.get("task"):
                phase += f" {_plain(event['task'])}/{_plain(event['condition'])} r{event['repeat']}"
            cases = f"{event.get('panel_measured', 0)}/{event['panel_total']}" if event.get('panel_total') else "-"
            width = max(20, shutil.get_terminal_size(fallback=(120, 24)).columns) - 1
            prefix = f"ARI | candidate {candidate}/{limit} | "
            counters = (
                f" | starts {event.get('evaluations_started', 0)}/{event.get('evaluation_limit', '?')}"
                f" | measured {cases}"
            )
            # When the line is too wide, shorten the phase text and keep the counters.
            phase_width = max(0, width - len(prefix) - len(counters))
            first = prefix + phase[:phase_width] + counters
            metrics = event.get("metrics") or {}
            latest = " | ".join(f"{_plain(name)}={value:.5g}" for name, value in metrics.items())
            second = "Latest measured: " + (latest or "not available yet")
            if self._drawn:
                self.stream.write("\r\x1b[1A")
            self.stream.write("\r\x1b[2K" + first[:width] + "\n\x1b[2K" + second[:width])
            self.stream.flush()
            self._drawn = True

    def close(self):
        with self._lock:
            if self._drawn and not self._closed:
                self.stream.write("\n")
                self.stream.flush()
            self._closed = True
