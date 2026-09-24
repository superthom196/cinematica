"""The provider pool: lives in the Cinematica process, one per installed
provider, and owns every subprocess that runs that provider's code.

A provider is someone else's Python, launched by path, running unsandboxed --
though, where the installer created one, under its own account (see
_worker_argv) rather than the service account's.
Everything here exists to make sure a bug in it costs one call, not the
process it runs in and not every OTHER call queued behind it: a bounded
worker count (so nothing here ever forks its way into resource exhaustion), a
deadline and a size cap on every response, and a hard rule that a worker
mid-request is never touched, which is what lets a config edit or a crash
elsewhere leave a film that is mid-playback alone.
"""
import collections
import json
import os
import pwd
import queue
import subprocess
import sys
import threading
import time

from . import contract

# Over this, a response is treated the same as a crash: something this
# malformed did not come from a provider that is merely slow or wrong about a
# title, and reading further would mean buffering an unbounded amount of a
# subprocess's output in this process's memory.
RESPONSE_CAP_BYTES = 16 * 1024 * 1024
# Bounded so a chatty provider (a debug build, a library that logs every
# request) cannot grow this process's memory forever just by staying alive.
STDERR_RING_LINES = 200
_READ_CHUNK = 65536

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../server

# Sentinels pushed onto a worker's response queue by its reader thread.
# Distinct objects, not strings/None, so they can never collide with a
# provider's own (id-bearing) JSON response, which is the whole reason call()
# can tell "the process is gone" apart from "the process said something".
_EOF = object()
_BADJSON = object()
_OVERSIZE = object()


class _OversizeLine(Exception):
    pass


def _read_line_capped(stream, cap):
    """One newline-terminated line from a binary stream, abandoned past `cap`
    bytes instead of read to completion.

    stdlib readline() has no size limit. A worker that emits one unterminated,
    multi-gigabyte line -- a runaway json.dumps, or just garbage -- would
    otherwise grow this process's memory without bound waiting for a newline
    that may never arrive. Reading in fixed chunks means the cap is enforced
    as the bytes arrive, not after they have already been buffered.
    """
    chunks, total = [], 0
    while True:
        chunk = stream.readline(_READ_CHUNK)
        if chunk == b"":
            return b"".join(chunks)  # EOF, whatever partial line came before it
        chunks.append(chunk)
        total += len(chunk)
        if chunk.endswith(b"\n"):
            return b"".join(chunks)
        if total > cap:
            raise _OversizeLine()


def _minimal_env():
    """PATH/HOME/LANG plus a PYTHONPATH pointing at server/ -- nothing else.

    The parent process's environment holds this box's own credentials and
    whatever else server.py's .env loaded into os.environ. None of that is
    this subprocess's business: it runs a third-party package, and passing
    the full environment would hand every installed provider every OTHER
    provider's credentials along with the server's own.
    """
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env["PYTHONPATH"] = _SERVER_DIR
    return env


def provider_user():
    """The account provider code runs as, or "" to run it as this process's.

    install.sh creates it (cinematica-provider) and names it in the unit. The
    service account is in the docker group, which is root on the machine, and
    a provider running as that account inherits it; this account is not. It
    is a smaller blast radius, not a sandbox: the provider still has the
    network and can read whatever on the box is world-readable.
    """
    return os.environ.get("CINEMATICA_PROVIDER_USER", "").strip()


def _worker_argv(package_dir):
    """(argv, env) that starts one worker, as provider_user() when one is set.

    sudo resets the environment, so the child's is handed over through
    env(1) on the command line instead, with HOME pointed at the provider
    account's own home: the service account's is not writable to it. The
    sudoers rule install.sh writes allows exactly this drop, and -n makes a
    missing rule fail at once, onto stderr and the web page, instead of
    waiting on a password prompt that nobody will ever answer.

    sudo stays as this worker's parent process, and that matters: this
    process may signal sudo (same real uid) but not the worker beneath it
    (another account). close()'s SIGTERM is relayed by sudo; kill()'s SIGKILL
    cannot be relayed, and reaches the worker through the parent-death signal
    host.py arms before loading any provider code.
    """
    argv = [sys.executable, "-m", "providers.host", package_dir]
    env = _minimal_env()
    user = provider_user()
    if not user:
        return argv, env
    env["HOME"] = pwd.getpwnam(user).pw_dir
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # server/ is not the provider account's to write in
    env["CINEMATICA_DIE_WITH_PARENT"] = "1"  # see host._die_with_parent
    passed = ["%s=%s" % kv for kv in sorted(env.items())]
    return ["sudo", "-n", "-u", user, "--", "env", "-i"] + passed + argv, env


class _Worker:
    """One provider subprocess plus the threads that keep it from ever
    blocking this process: a reader that turns its stdout into queued
    responses, and a drain that keeps its stderr pipe from filling up and
    deadlocking the provider's own write() calls."""

    def __init__(self, wid, package_dir, config, rev, stderr_ring, stderr_lock):
        self.id = wid
        self.config = config
        self.rev = rev
        self.responses = queue.Queue()
        self._req_seq = 0
        self._send_lock = threading.Lock()
        self._closed = False
        self._stderr_ring = stderr_ring
        self._stderr_lock = stderr_lock

        argv, env = _worker_argv(package_dir)
        self.proc = subprocess.Popen(
            argv,
            cwd=package_dir,
            env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def next_id(self):
        self._req_seq += 1
        return self._req_seq

    def send(self, obj):
        line = (json.dumps(obj, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        with self._send_lock:  # one writer at a time; call() already ensures one in-flight request
            self.proc.stdin.write(line)
            self.proc.stdin.flush()

    def _read_loop(self):
        try:
            while True:
                try:
                    raw = _read_line_capped(self.proc.stdout, RESPONSE_CAP_BYTES)
                except _OversizeLine:
                    self.responses.put(_OVERSIZE)
                    return
                if raw == b"":
                    return  # EOF: the process exited or closed stdout
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    self.responses.put(_BADJSON)
                    continue
                self.responses.put(msg)
        finally:
            # Always pushed, on every exit path from this loop, so a call()
            # blocked on responses.get() is guaranteed to be woken even if it
            # is waiting for a request that will now never be answered.
            self.responses.put(_EOF)

    def _drain_stderr(self):
        try:
            for raw in self.proc.stderr:
                try:
                    text = raw.decode("utf-8", "replace").rstrip("\n")
                except Exception:
                    continue
                if not text:
                    continue
                with self._stderr_lock:
                    # Redacted before storage, not on read: recent_stderr() can
                    # be printed straight into a log or the web UI with no
                    # second filtering pass to remember.
                    self._stderr_ring.append(contract.redact(text))
        except Exception:
            pass

    def kill(self):
        # Reaped here as well as killed: it marks the worker closed, so the
        # close() every caller follows it with returns at once and never
        # waits -- and each timed-out call used to leave a zombie behind.
        self._closed = True
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class Pool:
    """N persistent workers for one provider. `call()` checks one out, uses
    it for exactly one request/response round trip, and checks it back in --
    that checkout queue IS the concurrency bound. If every worker is busy,
    the caller waits; this never spawns a worker beyond `workers` to relieve
    that wait, because the bound exists to cap how much of this provider's
    (arbitrary, untrusted) code can run at once.
    """

    def __init__(self, provider_id, package_dir, config, config_rev, workers=3):
        self.provider_id = provider_id
        self.package_dir = package_dir
        self._lock = threading.RLock()
        self._config = config
        self._config_rev = config_rev
        self._n_workers = workers
        self._idle = queue.Queue()
        self._workers = {}
        self._next_id = 0
        self._closed = False
        self._stderr_lock = threading.Lock()
        self._stderr_ring = collections.deque(maxlen=STDERR_RING_LINES)
        for _ in range(workers):
            self._idle.put(self._spawn_locked())

    # -- worker lifecycle ------------------------------------------------------
    def _spawn_locked(self):
        with self._lock:
            self._next_id += 1
            w = _Worker(self._next_id, self.package_dir, self._config, self._config_rev,
                        self._stderr_ring, self._stderr_lock)
            self._workers[w.id] = w
            return w

    def _discard(self, worker):
        with self._lock:
            self._workers.pop(worker.id, None)
        worker.close()

    def _spawn_replacement_async(self):
        # Never awaited by the caller that hit the crash/timeout: that caller
        # already has its answer (an exception) and returning it promptly
        # matters more than this pool being back at full strength immediately.
        # A relaunch is a subprocess start plus manifest validation -- slow
        # enough that blocking every future call() behind it would turn one
        # bad worker into a stall for the whole provider.
        def _relaunch():
            try:
                with self._lock:
                    if self._closed:
                        return
                    w = self._spawn_locked()
                self._idle.put(w)
            except Exception as exc:
                # No caller is left to hand this to -- the original crash/
                # timeout already raised its own ProviderError. Surface it to
                # stderr so a provider that cannot be relaunched at all (its
                # package dir got deleted, its interpreter is broken) is at
                # least visible instead of silently shrinking this pool by one
                # worker forever.
                print("providers.runner: failed to relaunch worker for %r: %s"
                      % (self.provider_id, exc), file=sys.stderr)
        threading.Thread(target=_relaunch, daemon=True).start()

    def _checkout(self):
        while True:
            worker = self._idle.get()  # blocks: this line IS the concurrency bound
            with self._lock:
                if worker.id not in self._workers:
                    continue  # discarded while queued (closed pool, etc.) -- try the next one
                if worker.rev != self._config_rev:
                    # Idle, so nothing mid-request is being disturbed: swap it
                    # for a freshly configured worker before it takes this (or
                    # any) job, rather than answering under a stale account.
                    self._discard(worker)
                    worker = self._spawn_locked()
            return worker

    def _checkin(self, worker):
        self._idle.put(worker)

    # -- calls ------------------------------------------------------------------
    def call(self, op, params, timeout):
        if self._closed:
            raise contract.ProviderError(contract.E_CRASH, "pool is closed", provider=self.provider_id, op=op)

        worker = self._checkout()
        req_id = worker.next_id()
        try:
            worker.send({"id": req_id, "op": op, "config": worker.config, "params": params})
        except Exception:
            # The pipe is already broken -- nothing will ever answer this id.
            self._discard(worker)
            self._spawn_replacement_async()
            raise contract.ProviderError(contract.E_CRASH, "provider process is gone",
                                          provider=self.provider_id, op=op)

        try:
            msg = worker.responses.get(timeout=timeout)
        except queue.Empty:
            # SIGKILL, not a polite terminate: a hung worker cannot be trusted
            # to ever notice this request was abandoned, and a late reply
            # arriving after we have moved on would collide with whatever
            # request reuses this worker's id sequence next.
            worker.kill()
            self._discard(worker)
            self._spawn_replacement_async()
            raise contract.ProviderError(contract.E_TIMEOUT, "%s timed out after %.1fs" % (op, timeout),
                                          provider=self.provider_id, op=op)

        if msg is _EOF:
            self._discard(worker)
            self._spawn_replacement_async()
            raise contract.ProviderError(contract.E_CRASH, "provider process exited",
                                          provider=self.provider_id, op=op)
        if msg is _OVERSIZE:
            worker.kill()
            self._discard(worker)
            self._spawn_replacement_async()
            raise contract.ProviderError(contract.E_PROTOCOL,
                                          "provider response exceeded %d bytes" % RESPONSE_CAP_BYTES,
                                          provider=self.provider_id, op=op)
        if msg is _BADJSON or not isinstance(msg, dict) or msg.get("id") != req_id:
            worker.kill()
            self._discard(worker)
            self._spawn_replacement_async()
            raise contract.ProviderError(contract.E_PROTOCOL, "provider sent a malformed response",
                                          provider=self.provider_id, op=op)

        # A clean round trip on the id we asked about: this worker is healthy
        # and goes back to idle. Staleness (see _checkout) is checked on the
        # way OUT next time, never here -- there is no point discarding a
        # worker that just proved itself, only to relaunch an identical one.
        self._checkin(worker)

        if msg.get("ok"):
            return msg.get("result")
        err = msg.get("error") or {}
        raise contract.ProviderError(err.get("code") or contract.E_INTERNAL, err.get("message") or "",
                                      provider=self.provider_id, op=op)

    # -- config + shutdown --------------------------------------------------------
    def retire_on_idle(self, new_config, new_rev):
        """Point future work at a new config/rev without touching a worker
        that is mid-request right now.

        Nothing is killed here. A worker's `.rev` is fixed at the moment it
        was spawned; bumping the pool's own rev just means the NEXT time
        _checkout() dequeues that worker it will see a mismatch and swap it
        out then -- which is guaranteed to be after any request it is
        currently running has finished. That is what keeps a settings-page
        edit from reaching into a worker that is mid-stream-lookup for a film
        someone is watching right now.
        """
        with self._lock:
            self._config = new_config
            self._config_rev = new_rev

    def recent_stderr(self):
        with self._stderr_lock:
            return list(self._stderr_ring)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            workers = list(self._workers.values())
            self._workers.clear()
        try:
            while True:
                self._idle.get_nowait()
        except queue.Empty:
            pass
        for w in workers:
            w.close()


class PoolManager:
    """One Pool per installed provider, created on demand. Thread-safe: the
    web UI's config-save handler and a browse/playback request against the
    same provider run on different HTTP-server threads and both reach this.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._pools = {}

    def get_or_create(self, provider_id, package_dir, config, config_rev, workers=3):
        with self._lock:
            pool = self._pools.get(provider_id)
            if pool is None:
                pool = Pool(provider_id, package_dir, config, config_rev, workers=workers)
                self._pools[provider_id] = pool
            return pool

    def get(self, provider_id):
        with self._lock:
            return self._pools.get(provider_id)

    def reconfigure(self, provider_id, new_config, new_rev):
        with self._lock:
            pool = self._pools.get(provider_id)
        if pool is not None:
            pool.retire_on_idle(new_config, new_rev)

    def remove(self, provider_id):
        with self._lock:
            pool = self._pools.pop(provider_id, None)
        if pool is not None:
            pool.close()

    def close_all(self):
        with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
        for pool in pools:
            pool.close()
