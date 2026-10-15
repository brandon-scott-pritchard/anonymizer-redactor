"""A local language model, asked two things the rules cannot settle alone.

At review, which of the names found are the same real person or business -
"HARRlNGTON" and "Harringten" are OCR's Harrington, "Bill" is William - so one
person gets one invented name everywhere (see :mod:`redactor.llm_grouping`).
After the final check, what on a flagged page is still a real name, quoted
word for word, so it can be fixed and the page read again (see
:mod:`redactor.audit`).

The model runs as a separate local service (``mlx_lm.server``, an OpenAI-style
chat API) in its own folder with its own Python, offline - its weights are on
disk and it is never allowed to reach the network - and nothing is ever sent
anywhere but 127.0.0.1. It is optional: where it is missing every step runs
as it did without it and says so.

It is large - some twenty gigabytes - beside Kev (ten) and the OCR readers
(twelve), on a Mac with forty-eight. So it is not started with the app, and
never loaded beside Kev: each step that needs it takes it through the model
slot (:mod:`redactor.models`), which stops Kev first. Left alone, the
service also stops itself once nothing has held it for :data:`IDLE_SECONDS`.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

LLM_MODEL = "mlx-community/Qwen3.6-35B-A3B-4bit"
# a twenty-gigabyte model off a cold disk; a step that cannot wait longer
# goes on without it and says so
LLM_START_SECONDS = 180
# stopped once nothing has asked it for this long, so it is not resident
# beside the OCR readers while a set is read or run
IDLE_SECONDS = 300
REQUEST_SECONDS = 600


class LlmError(RuntimeError):
    """The model could not be asked, or did not answer in JSON."""


def llm_python() -> Path:
    return Path(os.environ.get("REDACTOR_LLM_PYTHON",
                               Path.home() / "Coding" / "llm" / ".venv" / "bin" / "python")
                ).expanduser()


def llm_model() -> str:
    return os.environ.get("REDACTOR_LLM_MODEL") or LLM_MODEL


def _free_port() -> int:
    from .audit import _free_port as free
    return free()


class LlmService:
    """The local model, started when a step needs it and stopped when idle.

    Started in its own process group and watched, as Kev is: a small watcher
    process stops it the moment the app's process is gone, so an app that
    crashed or was force-quit cannot leave a model holding twenty gigabytes.
    """

    def __init__(self, idle_seconds: float | None = None) -> None:
        self.process: subprocess.Popen | None = None
        self.watcher: subprocess.Popen | None = None
        self.port: int | None = None
        self.note = "not started"
        self.ready = threading.Event()
        self.idle_seconds = idle_seconds
        self.holders = 0
        self.last_used = time.time()
        self.lock = threading.RLock()

    @property
    def url(self) -> str | None:
        alive = self.process is not None and self.process.poll() is None
        return f"http://127.0.0.1:{self.port}" if alive and self.ready.is_set() and self.port \
            else None

    def start(self) -> bool:
        """Start the model in the background; False when there is none to start.
        Started once: a second call while it runs or starts changes nothing."""
        with self.lock:
            self.last_used = time.time()
            if self.process is not None and self.process.poll() is None:
                return True
            if os.environ.get("REDACTOR_LLM", "1") == "0":
                self.note = "turned off (REDACTOR_LLM=0)"
                return False
            if os.name == "nt":
                self.note = "not available on Windows"
                return False
            python = llm_python()
            if not python.is_file():
                self.note = f"not installed (no {python})"
                return False
            self.port = _free_port()
            self.ready.clear()
            env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                       HF_DATASETS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
            env.pop("PYTHONHOME", None)            # a frozen app's own, not the model's
            env.pop("PYTHONPATH", None)
            self.process = subprocess.Popen(
                [str(python), "-m", "mlx_lm.server", "--model", llm_model(),
                 "--host", "127.0.0.1", "--port", str(self.port)],
                cwd=str(python.parent), env=env, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # the watcher: when this process is gone, so is the model
            self.watcher = subprocess.Popen(
                ["/bin/sh", "-c",
                 f"while kill -0 {os.getpid()} 2>/dev/null && kill -0 {self.process.pid}"
                 f" 2>/dev/null; do sleep 2; done; kill -TERM -{self.process.pid} 2>/dev/null;"
                 f" sleep 5; kill -KILL -{self.process.pid} 2>/dev/null"],
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            atexit.register(self.stop)
            self.note = "starting"
            process = self.process
        threading.Thread(target=self._wait_until_up, args=(process,), daemon=True,
                         name="llm-start").start()
        threading.Thread(target=self._stop_when_idle, args=(process,), daemon=True,
                         name="llm-idle").start()
        return True

    def _wait_until_up(self, process) -> None:
        started = time.time()
        while self.process is process:
            if process.poll() is not None:
                self.note = "stopped while starting"
                return
            try:
                with _local_opener().open(f"http://127.0.0.1:{self.port}/v1/models",
                                          timeout=2) as response:
                    if response.status == 200:
                        self.note = f"running ({llm_model()}, offline)"
                        self.ready.set()
                        return
            except (urllib.error.URLError, OSError):
                pass
            if time.time() - started > LLM_START_SECONDS:
                self.note = "slow to start - still loading"
            time.sleep(1)

    def _idle_limit(self) -> float:
        return IDLE_SECONDS if self.idle_seconds is None else self.idle_seconds

    def _stop_when_idle(self, process) -> None:
        """Stop the model once nothing has held it for the idle time."""
        while self.process is process:
            time.sleep(max(0.2, min(5.0, self._idle_limit() / 4)))
            with self.lock:
                if self.process is not process:
                    return
                idle = (not self.holders
                        and time.time() - self.last_used >= self._idle_limit())
                if idle:
                    self.stop()
                    self.note = "stopped when idle - started again when needed"
                    return

    @contextmanager
    def held(self):
        """Keep the model up while a step asks it: never stopped as idle
        while held, and the idle time counts from when the hold ends."""
        with self.lock:
            self.holders += 1
            self.last_used = time.time()
        try:
            yield self
        finally:
            with self.lock:
                self.holders -= 1
                self.last_used = time.time()

    @property
    def starting(self) -> bool:
        """On its way up: running, not answering yet."""
        return (self.process is not None and self.process.poll() is None
                and not self.ready.is_set())

    def stop(self) -> None:
        with self.lock:
            process, self.process = self.process, None
            self.ready.clear()
            watcher, self.watcher = self.watcher, None
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, 15)
                process.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(process.pid, 9)
                except Exception:
                    pass
        if watcher is not None and watcher.poll() is None:
            watcher.terminate()
        if self.note.startswith(("running", "starting", "slow to start")):
            self.note = "stopped"


LLM = LlmService()


def wait_for_llm(seconds: float) -> str | None:
    """The model's address once it is up, or None if it is not within
    ``seconds``. A model that is not running - never started, or gone - is
    not waited for."""
    deadline = time.time() + seconds
    while LLM.starting and time.time() < deadline:
        LLM.ready.wait(timeout=1.0)
    return LLM.url


# --------------------------------------------------------------------------
# asking
# --------------------------------------------------------------------------


def _local_opener():
    """An opener that ignores any proxy settings: a request for 127.0.0.1
    sent through a proxy would leave the machine."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_local(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "http" and parsed.hostname == "127.0.0.1"


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def parse_json(reply: str):
    """The JSON in a model's reply.

    The reply may carry a reasoning block, a fenced code block, or a line of
    prose before the JSON; the first whole JSON object or array is taken.
    """
    text = _THINK.sub("", reply or "")
    if "</think>" in text:                     # the opening tag was in the template
        text = text.split("</think>", 1)[1]
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char in "{[":
            try:
                return decoder.raw_decode(text, index)[0]
            except ValueError:
                continue
    raise LlmError("the model did not answer in JSON")


def _post(url: str, body: dict, timeout: float) -> str:
    request = urllib.request.Request(f"{url}/v1/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with _local_opener().open(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LlmError(f"the model could not be asked ({exc})") from exc
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError("the model's answer was not a chat reply") from exc


def chat(url: str, system: str, user: str, schema_hint: str = "", max_tokens: int = 2048,
         temperature: float = 0, timeout: float = REQUEST_SECONDS):
    """Ask the local model and return the JSON it answers with.

    The fixed instructions go first and the material last, so a run of
    questions that share their opening reuses the server's prompt cache.
    One answer that is not JSON is asked again once; a second raises.
    """
    if not _is_local(url):
        raise LlmError("only a model on this computer is ever asked")
    instructions = system
    if schema_hint:
        instructions += "\n\nAnswer with JSON only, exactly in this shape: " + schema_hint
    messages = [{"role": "system", "content": instructions},
                {"role": "user", "content": user}]
    body = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature,
            "stream": False,
            # the model thinks aloud by default - slow, and not wanted here
            "chat_template_kwargs": {"enable_thinking": False}}
    reply = _post(url, body, timeout)
    try:
        return parse_json(reply)
    except LlmError:
        pass
    retry = dict(body, messages=messages + [
        {"role": "assistant", "content": reply},
        {"role": "user", "content": "That was not valid JSON. Answer again with the JSON "
                                    "only, nothing before or after it."}])
    return parse_json(_post(url, retry, timeout))
