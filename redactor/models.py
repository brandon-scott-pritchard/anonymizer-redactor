"""One model loaded at a time: Kev or the local language model, never both.

Kev holds some ten gigabytes and the local model some twenty, on a Mac with
forty-eight beside the OCR readers. So neither is started with the app: each
step that needs one asks this slot for it. The slot lets one step in at a
time, in the order they asked; stops whichever model is loaded if it is the
other one, and waits for its process to be gone; starts the one wanted and
waits for it to answer. On release the model stays loaded - the next step
very likely wants the same one - until another is wanted, or until nobody
has asked for IDLE_SECONDS.

The slot only sequences the two services (audit.KevService, llm.LlmService);
each still starts offline, in its own process group, watched so it dies
with the app.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

IDLE_SECONDS = 120


def _services() -> dict:
    """Looked up on every use, so a test's stand-in is the one sequenced."""
    from . import audit, llm

    return {"kev": audit.KEV, "llm": llm.LLM}


def _start_seconds(name: str) -> float:
    from . import audit, llm

    return audit.KEV_START_SECONDS if name == "kev" else llm.LLM_START_SECONDS


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _stop(service) -> None:
    """Stop a model and wait until its process is gone."""
    process = service.process
    service.stop()
    if process is not None:
        try:
            process.wait(timeout=15)
        except Exception:
            pass
        deadline = time.time() + 15
        while _alive(process.pid) and time.time() < deadline:
            time.sleep(0.1)


class Lease:
    """One step's turn at the slot."""

    def __init__(self, slot: "ModelSlot") -> None:
        self.slot = slot
        self.held = None                           # the service's own hold, if it has one
        self.released = False

    def load(self, name: str) -> str | None:
        """The model ``name`` loaded - the other one stopped first - and its
        address; None when it cannot be started or does not answer in time."""
        return self.slot._load(self, name)

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.slot._release(self)


class ModelSlot:
    def __init__(self, idle_seconds: float | None = None) -> None:
        self.idle_seconds = idle_seconds
        self.cond = threading.Condition()
        self.line: list[Lease] = []                # the holder first, then the waiting
        self.loaded = None                         # the service this slot last loaded
        self.turns = 0
        self.notes: dict[str, str] = {}

    def _idle_limit(self) -> float:
        return IDLE_SECONDS if self.idle_seconds is None else self.idle_seconds

    # ---------------------------------------------------------- turns --
    def acquire(self, on_wait=None) -> Lease:
        """A turn at the slot, after every step that asked before. ``on_wait``
        is called once if another step has it now."""
        lease = Lease(self)
        with self.cond:
            self.line.append(lease)
            self.turns += 1
            if self.line[0] is not lease and on_wait is not None:
                on_wait()
            while self.line[0] is not lease:
                self.cond.wait()
        return lease

    @property
    def busy(self) -> bool:
        with self.cond:
            return bool(self.line)

    @contextmanager
    def use(self, name: str, on_wait=None):
        """``with SLOT.use("llm") as url:`` - the model loaded for the block."""
        lease = self.acquire(on_wait)
        try:
            yield lease.load(name)
        finally:
            lease.release()

    # -------------------------------------------------------- loading --
    def _load(self, lease: Lease, name: str) -> str | None:
        services = _services()
        wanted = services[name]
        self._let_go(lease)
        for other_name, other in services.items():
            if other_name != name and other.process is not None:
                _stop(other)
        self.loaded = wanted
        if not wanted.start():
            self.notes[name] = wanted.note
            return None
        if hasattr(wanted, "held"):                # never stopped as idle while in use
            lease.held = wanted.held()
            lease.held.__enter__()
        deadline = time.time() + _start_seconds(name)
        while wanted.starting and time.time() < deadline:
            wanted.ready.wait(timeout=1.0)
        self.notes[name] = wanted.note
        return wanted.url

    def _let_go(self, lease: Lease) -> None:
        if lease.held is not None:
            held, lease.held = lease.held, None
            held.__exit__(None, None, None)

    def _release(self, lease: Lease) -> None:
        self._let_go(lease)
        with self.cond:
            if lease in self.line:
                self.line.remove(lease)
            turn = self.turns
            self.cond.notify_all()
        timer = threading.Timer(self._idle_limit(), self._stop_if_idle, args=(turn,))
        timer.daemon = True
        timer.start()

    def _stop_if_idle(self, turn: int) -> None:
        with self.cond:
            if self.line or self.turns != turn:
                return                             # asked for since
            loaded, self.loaded = self.loaded, None
            if loaded is not None and loaded.process is not None:
                _stop(loaded)

    def note(self, name: str) -> str:
        return self.notes.get(name) or _services()[name].note


SLOT = ModelSlot()
