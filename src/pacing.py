"""How a worker paces its requests: one backoff shape, shared.

Three of the four workers keep an inter-request delay that they move in two
directions: a refusal means they are asking too often and the delay doubles, and
healthy operation means they were asking too rarely and the delay shrinks. The
translator is deliberately not one of them — it waits out a daily quota, not a
rate, and `src/translator.py` says so.

**The decay is measured in time, not in successes.** The delay halves for every
ten minutes in which the worker was operating without being refused, so it is
the same wall-clock recovery whatever the current delay happens to be. A
success-counted rule cannot do that: 200 successes is twenty minutes of web
scraping at a 6 s delay and under a second of API calls, so the two workers
would drift apart for no reason anyone could act on.

`elapsed` comes from `time.monotonic`, kept in memory per worker. It is
deliberately not wall-clock time and deliberately not persisted: a daemon that
was stopped for a day must resume at the delay it had reached, not decide that a
day of downtime was a day of healthy operation and drop to the floor.

**A sustained failure needs no cap.** The delay doubles only when an attempt
fails, and the next attempt is a whole delay away, so the attempts spread
themselves out as fast as the delay grows. The delay after `k` refusals is
`d0 * 2**k` and the elapsed time to reach it is `d0 * (2**k - 1)` — the delay
tracks the length of the outage rather than running away from it. An hour of
continuous refusal from a 6 s delay reaches roughly an hour, not a year. The
ceilings that used to bound this were a defence against delays being moved by
the wrong signal, and bounding the wait is not the same job as bounding the
delay; see issue 21 in docs/code-issues.md.

**The delay is a float and only the copy on disk is rounded.** A step at a small
delay is far below what two decimal places can express, so rounding in memory
would silently stop the decay: at a 0.5 s delay one step is ~0.0006 s, and
`round(0.4994, 3)` is `0.5`. Precision is kept where the arithmetic happens and
given up where it is only a restart point.

**The delay is state, not configuration.** Each of the three workers owns a
section of the daemon state file beside the database -- `src/daemon_state.py`,
the same restart-surviving store the translator's backoff uses -- and writes its
delay there as it moves. It is not read from `config.yaml`: the delay describes
what the daemon is currently doing about a condition that will pass, not a
setting anyone chose, and an operator resetting one deletes its section rather
than editing a Python file or restarting anything unusual. The sections are
separate so one worker's write cannot clobber another's, which is the merge
property `StateStore.save` provides.
"""

import time

# The daemon-state section each rate-seeking worker owns. One per worker, so a
# write by one cannot lose another's delay.
API_DELAY_SECTION = "api_delay"
WEB_DELAY_SECTION = "web_delay"
IMAGE_DELAY_SECTION = "image_delay"

# The delay halves for every this many seconds of healthy operation. Ten minutes
# is the owner's figure, chosen so every queue recovers from a doubling over the
# same wall-clock window rather than over the same number of attempts.
HALF_LIFE_SECONDS = 600.0

# A refusal means asking twice as seldom. Doubling is the standard congestion
# response: it halves the request rate, which is the quantity actually limited.
BACKOFF_FACTOR = 2.0

# How far the delay must move before the new value is worth a state-file write.
# The decay now runs on every success, so without a step of its own it would
# rewrite the state file per request -- the failure mode the old 100-success
# rule happened to avoid.
PERSIST_STEP_SECONDS = 0.05

# The persisted value is a restart point, not the working value, so it does not
# need the precision the arithmetic does.
PERSIST_DECIMALS = 2

# How long a loop waits after losing a SQLite lock race before it tries again.
# The connection's busy timeout is 15 s, so reaching this pause means a writer
# held the lock longer than the timeout allowed; retrying at once would usually
# meet the same lock and spin. Every loop that meets a lock -- the daemon's main
# loop, the web scraper and the image downloader -- shares this one value.
DB_LOCK_RETRY_SECONDS = 5.0


def decay(delay: float, elapsed: float, floor: float,
          half_life: float = HALF_LIFE_SECONDS) -> float:
    """The delay after ``elapsed`` seconds of healthy operation.

    Halves over ``half_life`` seconds of elapsed time whatever the delay is,
    which is the property a success-counted rule cannot have.
    """
    if elapsed <= 0:
        return max(floor, delay)
    return max(floor, delay * 2.0 ** (-elapsed / half_life))


def backoff(delay: float) -> float:
    """The delay after a refusal. Uncapped by design; see the module docstring."""
    return delay * BACKOFF_FACTOR


def now() -> float:
    """The clock the decay is measured against.

    Monotonic because an interval is being measured and a clock correction must
    not be able to move a delay, and in memory only because downtime is not
    healthy operation.
    """
    return time.monotonic()


class Clock:
    """When the last attempt happened, so the decay can be measured in time.

    Advanced on *every* attempt, not only the healthy ones. If it moved only on
    success, the first success after a run of failures would see the whole run
    as elapsed time and collapse the delay to the floor in one step, which is
    the opposite of backing off.
    """

    def __init__(self):
        self._at = now()

    def since(self) -> float:
        """Seconds since the previous call, and start the next interval."""
        moment = now()
        elapsed = max(0.0, moment - self._at)
        self._at = moment
        return elapsed


def persistable(delay: float) -> float:
    """``delay`` as it should be written to disk."""
    return round(delay, PERSIST_DECIMALS)


def needs_persist(delay: float, last_persisted: float) -> bool:
    """Whether the delay has moved far enough to be worth a state write."""
    return abs(delay - last_persisted) >= PERSIST_STEP_SECONDS


def load_delay(store, section: str) -> float | None:
    """The delay ``section`` holds, or ``None`` when there is no usable one.

    ``store`` is a :class:`src.daemon_state.StateStore`, or ``None`` for a
    construction that has no state file (a test, or an embedded caller): both
    answer ``None``, which the caller turns into its default. A section holding
    a non-number, or one that is zero or negative, is treated as absent for the
    same reason ``config.get(key) or default`` did -- a delay of zero is not a
    rate, and a corrupt value must not become one by being trusted.

    The store already treats an unreadable file as empty, so this does not catch
    anything itself: a broken state file costs one default start, never a crash.
    """
    if store is None:
        return None
    try:
        value = store.load().get(section)
        delay = float(value)
    except (TypeError, ValueError, AttributeError):
        return None
    return delay if delay > 0 else None


def save_delay(store, section: str, delay: float) -> bool:
    """Persist ``delay`` into its own state-file ``section``.

    Only the copy on disk is rounded (:func:`persistable`); the working delay
    keeps its precision. A store of ``None`` is a no-op, so a worker constructed
    without one behaves exactly as it did before the state file existed.
    """
    if store is None:
        return False
    return store.save({section: persistable(delay)})


def wait(seconds: float, keep_running) -> bool:
    """Sleep, staying responsive to shutdown.

    ``keep_running`` is polled about once a second and the wait ends as soon as
    it answers false, so a long backoff cannot hold a stop or a pause for its
    whole duration. This does not shorten the delay: the caller sleeps exactly
    what it was given, it just refuses to become deaf while doing it.

    Returns whether the full period was served.
    """
    deadline = now() + max(0.0, seconds)
    while keep_running():
        remaining = deadline - now()
        if remaining <= 0:
            return True
        time.sleep(min(1.0, remaining))
    return False
