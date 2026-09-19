import time
import logging
import random
import re
import threading
from datetime import datetime, timezone
from openai import OpenAI
from src.database import get_connection, get_next_batch_for_translation
from src.wordlist import WORDS

logging.getLogger("httpx").setLevel(logging.WARNING)

# Backoff for a batch that fails. Growing the delay is what stops a persistent
# condition becoming a tight loop against the API, which is what happened before:
# the same batch was re-sent every ~1.2 s indefinitely.
#
# There are two shapes because the failures differ in kind. A transport error or
# a rate limit can clear on its own, so it starts low and caps at minutes. An
# account-level rejection -- credentials, billing, permissions -- cannot be fixed
# by waiting, so it starts at a minute and caps at an hour: a raised spend limit
# is then picked up without a restart, and the log stays quiet meanwhile.
RETRY_BASE_SECONDS = 2.0
RETRY_MAX_SECONDS = 300.0
ACCOUNT_BASE_SECONDS = 60.0
ACCOUNT_MAX_SECONDS = 3600.0

# Statuses meaning the account or key must change before another attempt can
# succeed. Everything else -- rate limits, server errors, connection failures --
# is worth retrying.
ACCOUNT_LEVEL_STATUSES = frozenset({401, 402, 403})

# The streak is persisted, so it is read back from a file that a crash, an
# editor or a bad disk can leave nonsense in. The exponent has to be bounded
# *before* it is used, because `2 ** (streak - 1)` on a value of a billion would
# build that integer before `min()` ever saw it. 2**63 seconds is already far
# beyond every cap here, so clamping costs nothing.
MAX_FAILURE_STREAK = 64

# The section this thread owns in the daemon state file. See src/daemon_state.py.
STATE_SECTION = "translation_backoff"

# How long a request is given to fill before it is sent anyway. The loop used to
# re-poll a partial batch every 30 s instead of ever sending it, so work smaller
# than a full batch never ran; this is a deadline for the first send, not an
# interval between polls.
BATCH_FILL_WAIT_SECONDS = 30.0

# Safety ceiling on items per request. The character cap below bounds a request's
# size; this one bounds how many rows a single bad reply can cost, which is what
# the fixed batch of 20 was doing before.
DEFAULT_BATCH_ITEMS = 20

# Upper bound on the source characters in one request, chosen from the queue's own
# distribution rather than guessed. *Measured* in the 2026-09-18 backup (127,385
# rows): median 19 characters, p90 69, p99 996, max 7,725; per field, `title_en`
# median 18, `short_description_en` median 29 (max 7,725) and
# `extended_description_en` median 42 (p99 4,072, max 7,157).
#
# 4,000 is about one p99 extended description. A batch of the short fields is
# therefore bounded by the item ceiling rather than this cap -- twenty titles at
# the median is ~360 characters -- while a long field is sent more or less alone.
# The longest field in the queue still fits in one request on its own, since the
# cap is only consulted from the second item onwards, but the 192 KB reply that a
# full batch of long descriptions produced cannot recur: the worst case becomes a
# single over-cap field, around 8 KB.
DEFAULT_BATCH_CHAR_CAP = 4000

# Translation is not a creative task, so sampling only adds variance to it.
DEFAULT_TEMPERATURE = 0.0


def backoff_delay(streak: int, retryable: bool) -> float:
    """Seconds to wait before the next attempt, after ``streak`` failures.

    Pure so the shape of the backoff can be tested without a thread, a clock or
    a network, and so the value that gets persisted comes from one place.
    """
    base = RETRY_BASE_SECONDS if retryable else ACCOUNT_BASE_SECONDS
    cap = RETRY_MAX_SECONDS if retryable else ACCOUNT_MAX_SECONDS
    streak = max(1, min(int(streak), MAX_FAILURE_STREAK))
    return float(min(base * (2 ** (streak - 1)), cap))


def _coerce_utc(value):
    """A timezone-aware UTC datetime from a stored value, or ``None``.

    Accepts a string or a datetime, because PyYAML resolves an ISO timestamp to
    a ``datetime`` on the way back in: both shapes arrive here from one file.
    Naive values are read as UTC rather than rejected.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_streak(value) -> int:
    """A usable streak from a stored value: 0 when unusable, never out of range."""
    try:
        streak = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(streak, MAX_FAILURE_STREAK))


def remaining_backoff(next_attempt_at, now: float, cap: float) -> float:
    """Seconds still to wait for a persisted ``next_attempt_at``.

    Zero when the moment has passed, when the value is unusable, or when it is
    further away than ``cap``: no legitimate backoff is longer than the cap, so
    a timestamp beyond it means a bad clock or an edited file, and honouring it
    would park the thread for an unexplained age.
    """
    when = _coerce_utc(next_attempt_at)
    if when is None:
        return 0.0
    return max(0.0, min(cap, when.timestamp() - now))


def retryable_failure(exc: BaseException) -> bool:
    """Whether another attempt could plausibly succeed.

    Transport failures carry no status code and are retryable. Authentication,
    billing and permission rejections are not: the account or the key has to
    change first, and retrying only adds noise.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        return True
    return status not in ACCOUNT_LEVEL_STATUSES


def _validate_openai_api_key(config: dict) -> str | None:
    openai_config = config.get("openai", {})
    api_key = openai_config.get("api_key")
    if api_key and "YOUR_OPENAI_API_KEY" not in api_key:
        return api_key
    return None


def _create_openai_client(openai_config: dict) -> OpenAI:
    return OpenAI(
        api_key=openai_config.get("api_key"),
        base_url=openai_config.get("endpoint", "https://api.openai.com/v1")
    )


def is_ascii(text: str) -> bool:
    if not text:
        return True
    return all(ord(c) < 128 for c in text)


class TranslationResponseError(ValueError):
    """A reply that yielded no usable translation block at all.

    Deliberately a ``ValueError`` and so carries no status code, which makes
    ``retryable_failure`` treat it as retryable: the request reached the model, so
    the next attempt may come back well formed. The caller owns the backoff -- see
    ``_register_failure`` -- and raising is what stops a model answering nothing
    but prose from being re-sent in a tight loop.
    """


# The boundary line that opens each block starts with a phrase of four ordinary
# words, drawn fresh for every request and copied verbatim by the model onto every
# block in that request. Words rather than a run of punctuation because punctuation
# costs a token per character where a word costs one token for three to five
# characters -- and because it is the phrase, not the shape of the line, that makes
# a boundary unambiguous. Ordinary prose contains dashes; it does not contain a
# nonce.
PHRASE_WORDS = 4

# Bounded so a pathological batch cannot spin. A four-word phrase occurs in the
# source text with probability around len(text)/2.8e12 per draw, so this is a
# formality rather than a mechanism.
PHRASE_ATTEMPTS = 8

# A word in the phrase, and a field label. Both allow an internal hyphen (`yo-yo`
# is one of the list's words) and neither allows digits, which is what separates
# the phrase from the id that follows it.
PHRASE_WORD_RE = r"[a-z]+(?:-[a-z]+)*"
FIELD_LABEL_RE = r"[a-z]+(?:_[a-z]+)*"

# A boundary whose phrase the model mangled: some short words, then an id and a
# field label. Consulted only after the phrase-anchored split has failed to produce
# one block per row, so a false positive cannot cost a reply that was otherwise
# well formed.
TOLERANT_BOUNDARY_RE = re.compile(
    rf"^\s*(?:{PHRASE_WORD_RE}\s+){{2,6}}(\d+)\s+({FIELD_LABEL_RE})\s*:?\s*$", re.IGNORECASE
)


def system_prompt(phrase: str) -> str:
    """The instructions, naming this request's boundary phrase.

    The phrase is ordinary English, so the model has to be told plainly that the
    boundary line is to be copied rather than translated -- otherwise a model will
    helpfully translate the phrase along with the content it prefixes.
    """
    return (
        "You translate Steam Workshop text into English. Preserve BBcode tags and "
        "the original formatting. Every block below begins with a boundary line "
        f'that starts with the phrase "{phrase}", followed by a number and a field '
        "name. Copy each boundary line exactly, character for character, including "
        "that phrase: do not translate it, and do not reorder the blocks. Translate "
        "only the text beneath each boundary line. Output nothing but the blocks."
    )


# The wire vocabulary: the label that names a queue field in a boundary line. One
# word each, because a label is repeated on every block of every request and a
# single token is the cheapest one can be. Written out rather than derived by
# trimming `_en`, so the vocabulary the model is shown is stated in one place and a
# renamed column cannot quietly change the wire.
FIELD_LABELS = {
    "title_en": "title",
    "short_description_en": "short",
    "extended_description_en": "long",
    "personaname_en": "user",
}


def field_label(field: str) -> str:
    """The boundary-line label for a queue field.

    ``short`` and ``long`` are deliberately different words. An item can have both
    descriptions queued at once, and the fallback alignment matches on
    ``(item_id, label)``, so collapsing them to one label would leave a mismatched
    block unattributable.

    A field with no alias is returned unchanged rather than raising: there is no
    fifth field today, but a future one must not break alignment. The fallback name
    is still unique per field and the reader accepts any lowercase label
    (``FIELD_LABEL_RE``), so such a field round-trips -- it is simply labelled with a
    longer word than the aliases.
    """
    return FIELD_LABELS.get(field, field)


def generate_phrase(rng=None) -> str:
    """``PHRASE_WORDS`` distinct words from the list, in random order.

    Takes a random source so a seeded one makes the phrase reproducible in a test;
    the thread passes nothing and draws from the module's own generator.
    """
    chooser = rng if rng is not None else random
    return " ".join(chooser.sample(WORDS, PHRASE_WORDS))


def choose_phrase(batch: list[dict], rng=None) -> str:
    """A phrase that does not occur in the text it is about to prefix.

    The words are ordinary English, so a batch could in principle contain the
    phrase; the check is a lower-cased substring test over the source texts, and a
    fresh phrase is drawn until one is clear or the attempt budget runs out. A batch
    that exhausts the budget keeps the last phrase rather than failing: the check
    makes a collision vanishingly unlikely, and refusing to translate would cost
    more than the residual ambiguity would.
    """
    haystack = "\n".join((row.get("original_text") or "") for row in batch).lower()
    phrase = generate_phrase(rng)
    for _ in range(PHRASE_ATTEMPTS - 1):
        if phrase not in haystack:
            return phrase
        phrase = generate_phrase(rng)
    if phrase in haystack:
        logging.debug(
            "Boundary phrase still occurs in the source text after %d draws.",
            PHRASE_ATTEMPTS,
        )
    return phrase


def boundary_re(phrase: str) -> re.Pattern:
    """The boundary line for one request: that request's phrase, then id and label."""
    return re.compile(
        rf"^\s*{re.escape(phrase)}\s+(\d+)\s+({FIELD_LABEL_RE})\s*:?\s*$", re.IGNORECASE
    )


def _row_key(row: dict) -> str:
    """A queue row's identity, for logs. Not a wire format -- see `boundary_line`."""
    return f"{row['item_type']}_{row['item_id']}_{row['field']}"


def boundary_line(row: dict, phrase: str) -> str:
    """The line that opens ``row``'s block, which the model is asked to copy."""
    return f"{phrase} {row['item_id']} {field_label(row['field'])}"


def wire_block(row: dict, phrase: str) -> str:
    """One field: its boundary line, then its source text.

    The request is written in the shape of the reply we want back, so translating
    is copying a structure rather than building one. That is the point: the JSON
    envelope this replaced required the model to escape every backslash and quote in
    the translated text, and the escapes it got wrong (`Invalid \\escape`,
    `Invalid \\uXXXX escape`) discarded the whole request -- 83 of 339 attempts,
    measured in the live log, each one billed and thrown away.
    """
    return f"{boundary_line(row, phrase)}\n{row.get('original_text') or ''}"


def build_wire_request(rows: list[dict], phrase: str) -> str:
    """The user-turn body: every field, in queue order, in reply shape."""
    blocks = "\n".join(wire_block(row, phrase) for row in rows)
    return (
        "Translate the text under each boundary line into English. Keep every "
        "boundary line exactly as it is.\n\n" + blocks
    )


def _blocks_from_reply(content: str, pattern: re.Pattern) -> list[tuple[int, str, str]]:
    """Split a reply into ``(item_id, field_label, text)`` blocks on ``pattern``.

    A block opens at a boundary line and runs to the next one. Its text is kept
    verbatim -- internal newlines and spacing included -- with only the newlines
    that delimit the block trimmed, because those are the boundary rather than the
    translation. Anything before the first boundary is ignored: a preamble ("Here
    are the translations:") belongs to no block.
    """
    blocks: list[tuple[int, str, str]] = []
    current: tuple[int, str, list[str]] | None = None
    for line in (content or "").split("\n"):
        matched = pattern.match(line)
        if matched:
            if current is not None:
                blocks.append((current[0], current[1], "\n".join(current[2]).strip("\n")))
            current = (int(matched.group(1)), matched.group(2).lower(), [])
        elif current is not None:
            current[2].append(line)
    if current is not None:
        blocks.append((current[0], current[1], "\n".join(current[2]).strip("\n")))
    return blocks


def split_blocks(content: str, phrase: str) -> list[tuple[int, str, str]]:
    """Split a reply on this request's phrase."""
    return _blocks_from_reply(content, boundary_re(phrase))


def _positional(blocks: list[tuple[int, str, str]]) -> dict[int, str]:
    """Assign blocks in the order they arrived, dropping the empty ones."""
    return {index: text for index, (_i, _f, text) in enumerate(blocks) if text}


def _aligned(blocks: list[tuple[int, str, str]], batch: list[dict]) -> dict[int, str]:
    """Assign blocks by ``(item_id, field label)``, ignoring ones matching no row."""
    by_key: dict[tuple[int, str], str] = {}
    for item_id, label, text in blocks:
        if text:
            by_key.setdefault((item_id, label), text)
    usable: dict[int, str] = {}
    for index, row in enumerate(batch):
        text = by_key.get((row["item_id"], field_label(row["field"])))
        if text:
            usable[index] = text
    return usable


def match_translations(content: str, batch: list[dict], phrase: str) -> dict[int, str]:
    """Map each batch row's position to its translated text.

    Positional first: when the reply yields exactly as many blocks as were sent,
    they are assigned in order and the boundaries are corroboration only. That is
    what keeps a reply usable when the model translates correctly but mangles a
    boundary -- the common failure, and the reason the count is consulted first. A
    reply whose phrase was mangled is then retried against the tolerant boundary
    pattern before any alignment is attempted.

    When the counts differ under both patterns, the boundaries become an alignment
    guide, matched on ``(item_id, field label)``. A row that gets no block is simply
    absent from the result: it is not deleted from `translation_queue` and a later
    pass picks it up. Blocks matching no row are ignored.

    Either way a partial reply is a partial success, never a reason to discard the
    blocks that did arrive.
    """
    on_phrase = split_blocks(content, phrase)
    if len(on_phrase) == len(batch):
        return _positional(on_phrase)

    tolerant = _blocks_from_reply(content, TOLERANT_BOUNDARY_RE)
    if len(tolerant) == len(batch):
        return _positional(tolerant)

    blocks = tolerant if len(tolerant) > len(on_phrase) else on_phrase
    return _aligned(blocks, batch)


def pack_batch(rows: list[dict], char_cap: int, item_ceiling: int) -> tuple[list[dict], bool]:
    """Take candidates, in queue order, into one request.

    The first candidate is taken **before** the cap is consulted, so a field larger
    than the cap is sent on its own rather than never being sent at all; from the
    second onwards the cap is checked **before** the candidate is taken, so no
    other batch exceeds it. The item ceiling applies throughout.

    Returns the batch and whether a candidate was left behind. That flag is what
    tells a full request from a starved one: it is set both when the cap refused
    the next candidate and when the ceiling stopped us, and clear only when the
    candidates ran out.
    """
    batch: list[dict] = []
    used = 0
    for row in rows:
        if len(batch) >= item_ceiling:
            return batch, True
        length = len(row.get("original_text") or "")
        if batch and used + length > char_cap:
            return batch, True
        batch.append(row)
        used += length
    return batch, False


def _positive_int(value, default: int) -> int:
    """A usable positive integer from config, or the default."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 1 else default


def _temperature(value) -> float:
    """A usable sampling temperature, clamped to the range the API accepts."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TEMPERATURE
    return min(2.0, max(0.0, number))


class TranslatorThread(threading.Thread):
    def __init__(self, config: dict, state_store=None):
        super().__init__(daemon=True)
        self.config = config
        self.db_path = config.get("database", {}).get("path", "workshop.db")
        openai_config = config.get("openai", {}) or {}
        # `batch` is the item ceiling and `batch_char_cap` the size cap; a request
        # is packed by both, so the smaller one binds. They are separate because
        # they bound different costs: the cap bounds what the model must emit in
        # one reply, the ceiling bounds how many rows one bad reply can strand.
        self.batch_size = _positive_int(openai_config.get("batch"), DEFAULT_BATCH_ITEMS)
        self.batch_char_cap = _positive_int(
            openai_config.get("batch_char_cap"), DEFAULT_BATCH_CHAR_CAP
        )
        self.temperature = _temperature(openai_config.get("temperature"))
        self.running = True
        self._failure_streak = 0
        # Injected, and optional, so the thread stays constructible and testable
        # without a filesystem. The daemon passes a StateStore so a backoff
        # outlives a restart; with none, behaviour is exactly as it was.
        self.state_store = state_store

    def run(self):
        openai_config = self.config.get("openai", {})
        if not _validate_openai_api_key(self.config):
            logging.warning("OpenAI API key not configured. Translation thread exiting.")
            return

        client = _create_openai_client(openai_config)
        model = openai_config.get("model", "gpt-4o-mini")
        logging.info("Starting batched translation background thread...")

        # A backoff that was still running when the daemon stopped is resumed,
        # not restarted: the condition it was waiting on did not change just
        # because the process did.
        self._resume_persisted_backoff()

        while self.running:
            candidates = self._read_candidates()
            if candidates is None:
                self._sleep(30)
                continue
            if not candidates:
                # Genuinely empty queue: keep the long idle poll.
                self._sleep(30)
                continue

            batch, had_more = pack_batch(candidates, self.batch_char_cap, self.batch_size)
            full = had_more or len(batch) >= self.batch_size
            urgent = any(row.get("priority", 0) >= 5 for row in batch)

            if not full and not urgent:
                # Starved: the queue handed over everything it had and it did not
                # fill the request. Give it one bounded window to grow, then send
                # what we have. Waiting again on a batch that did not grow is what
                # the old loop did forever -- it re-polled the same partial batch
                # every 30 s, so work smaller than a full batch never ran.
                self._sleep(BATCH_FILL_WAIT_SECONDS)
                refreshed = self._read_candidates()
                if refreshed:
                    # Whatever the queue holds now is what gets sent: the deadline
                    # has passed, so the re-pack feeds the send rather than another
                    # decision about whether to wait.
                    batch = pack_batch(refreshed, self.batch_char_cap, self.batch_size)[0]

            # A failed batch keeps its translation_queue rows, so backing off
            # costs nothing but time and the work is picked up again later.
            try:
                self._translate_batch(batch, client, model)
            except Exception as e:
                self._sleep(self._register_failure(e))
                continue
            # A reply that covered only part of the batch arrives here too, and
            # that is deliberate: the rows it missed are still queued, and growing
            # the failure streak for them would back off work the model did return.
            self._register_success()
            self._sleep(1)

        # Thread lifecycle logging handled by daemon

    def _read_candidates(self) -> list[dict] | None:
        """One fetch of a full request's worth of candidates, plus one.

        The extra row is what makes "the next candidate would exceed the cap"
        detectable at all: without it a full batch and a queue that has run dry
        look the same, and the loop could not tell whether waiting would help.
        Returns ``None`` when the read itself failed, which is not the same as an
        empty queue.
        """
        try:
            return get_next_batch_for_translation(self.db_path, limit=self.batch_size + 1)
        except Exception as e:
            logging.error(f"Translator thread error: {e}")
            return None

    def _sleep(self, seconds: float) -> None:
        """Sleep, but stay responsive to shutdown.

        A backoff can run to an hour, and one long sleep would hold the daemon's
        graceful shutdown for as long as it had left to run.
        """
        deadline = time.monotonic() + seconds
        while self.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def _resume_persisted_backoff(self) -> None:
        """Restore the recorded streak, then wait out the rest of its delay.

        Without this the delay restarts from its base on every daemon restart,
        so a condition that outlives a few restarts is never backed off far
        enough: an account-level rejection that had reached its hour would be
        retried a minute after each restart, and the log would say the same
        thing each time.
        """
        if self.state_store is None:
            return
        section = self.state_store.load().get(STATE_SECTION)
        if not isinstance(section, dict):
            return

        self._failure_streak = _coerce_streak(section.get("failure_streak"))
        if not self._failure_streak:
            return

        remaining = remaining_backoff(
            section.get("next_attempt_at"), time.time(), ACCOUNT_MAX_SECONDS
        )
        if remaining <= 0:
            logging.info(
                "Resuming translation with %d failed attempt(s) recorded; "
                "the last backoff has already expired.",
                self._failure_streak,
            )
            return
        logging.info(
            "Resuming translation backoff: %.0fs still to wait, after %d failed attempt(s).",
            remaining, self._failure_streak,
        )
        self._sleep(remaining)

    def _persist_backoff(self, delay: float, retryable: bool) -> None:
        """Record the streak and when the next attempt falls due.

        The streak is what reconstructs the delay if the timestamp is missing;
        the timestamp is what lets a restart wait only the remainder instead of
        serving the whole delay again. ``kind`` is for whoever reads the file,
        not for the arithmetic.
        """
        if self.state_store is None:
            return
        due = datetime.now(timezone.utc).timestamp() + delay
        self.state_store.save({STATE_SECTION: {
            "failure_streak": self._failure_streak,
            "next_attempt_at": datetime.fromtimestamp(due, timezone.utc).isoformat(),
            "kind": "retryable" if retryable else "account-level",
        }})

    def _register_failure(self, exc: BaseException) -> float:
        """Record a failed batch and return how long to wait before retrying."""
        retryable = retryable_failure(exc)
        self._failure_streak += 1
        delay = backoff_delay(self._failure_streak, retryable)
        kind = "retryable" if retryable else "account-level, not retryable"
        logging.error(
            "Batch translation failed (%s, attempt %d): %s — backing off %.0fs.",
            kind, self._failure_streak, exc, delay,
        )
        self._persist_backoff(delay, retryable)
        return delay

    def _register_success(self) -> None:
        """Clear the backoff once a batch gets through."""
        if not self._failure_streak:
            # Nothing outstanding. This runs after every batch, so the happy
            # path must not touch the disk.
            return
        logging.info(
            "Translation recovered after %d failed attempt(s).", self._failure_streak
        )
        self._failure_streak = 0
        if self.state_store is not None:
            self.state_store.remove(STATE_SECTION)

    def _translate_batch(self, batch: list[dict], client: OpenAI, model: str) -> tuple[int, int]:
        """Translate one request's worth of fields and store what came back.

        Returns ``(translated, left_queued)``. A reply covering only part of the
        batch is a success: the rows it missed keep their `translation_queue` rows
        and a later pass picks them up. A reply yielding no usable block at all
        raises, which is what puts the caller's backoff in charge of the retry
        instead of re-sending the same request in a tight loop.
        """
        if not batch:
            return (0, 0)

        phrase = choose_phrase(batch)
        prompt = build_wire_request(batch, phrase)
        now_ts = int(time.time())
        conn = get_connection(self.db_path)

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt(phrase)},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.temperature,
            )
            content = response.choices[0].message.content or ""
            if not isinstance(content, str):
                content = str(content)
            logging.debug(f"Raw translation response: {content[:500]}")

            translations = match_translations(content, batch, phrase)
            if not translations:
                # Zero usable blocks is a failure rather than an empty success:
                # every row is still queued, so the caller has to back off rather
                # than immediately re-send the same request.
                raise TranslationResponseError(
                    f"No usable translation blocks in a {len(batch)}-field "
                    f"response: {content[:200]!r}"
                )

            translated_count = 0
            translated_ids = set()
            for index, row in enumerate(batch):
                trans_text = translations.get(index)

                if not trans_text:
                    logging.warning(f"No translation returned for {_row_key(row)}")
                    continue

                if row["item_type"] == "user":
                    table, id_col = "users", "steamid"
                    # Users have no steam_updated_at, so this column holds OUR
                    # wall-clock time and is named translated_at, not a version.
                    stamp_column = "translated_at"
                    stamp_value = now_ts
                else:
                    table, id_col = "workshop_items", "workshop_id"
                    # Look up steam_updated_at for version tracking
                    stamp_column = "translate_version"
                    revision_row = conn.execute(
                        "SELECT steam_updated_at FROM workshop_items WHERE workshop_id = ?",
                        (row["item_id"],)
                    ).fetchone()
                    stamp_value = revision_row["steam_updated_at"] if revision_row and revision_row["steam_updated_at"] else now_ts

                conn.execute(
                    f"UPDATE {table} SET {row['field']} = ?, {stamp_column} = ? WHERE {id_col} = ?",
                    (trans_text, stamp_value, row["item_id"])
                )
                conn.execute("DELETE FROM translation_queue WHERE id = ?", (row["id"],))
                translated_count += 1
                translated_ids.add((row["item_type"], row["item_id"]))
                logging.debug(f"[{row['item_id']}] {row['field']}: \"{row['original_text'][:40]}\" → \"{trans_text[:40]}\"")

            for item_type, item_id in translated_ids:
                # Keyed by type as well as id: a steamid and a workshop_id are
                # both integers, so an unqualified id could count or complete a
                # row belonging to the other table.
                remaining = conn.execute(
                    "SELECT COUNT(*) as cnt FROM translation_queue "
                    "WHERE item_type=? AND item_id=?",
                    (item_type, item_id)
                ).fetchone()["cnt"]
                if remaining:
                    continue
                if item_type == "user":
                    # The per-field write above already stamped
                    # `users.translated_at` -- our clock is the only version a
                    # creator has, so completion needs no second stamp. Only the
                    # mirror is left, and clearing it here keeps
                    # `users.translation_priority` a mirror of the queue exactly
                    # as the item column is.
                    conn.execute(
                        "UPDATE users SET translation_priority = 0 WHERE steamid = ?",
                        (item_id,)
                    )
                    continue
                revision_row = conn.execute(
                    "SELECT steam_updated_at FROM workshop_items WHERE workshop_id = ?",
                    (item_id,)
                ).fetchone()
                stamp_value = revision_row["steam_updated_at"] if revision_row and revision_row["steam_updated_at"] else now_ts
                # translated_at is OUR clock, stamped when the item's last
                # queued field is gone -- the point at which the stage is
                # actually complete for this item. It is written in the same
                # statement (and so the same transaction) that zeroes the
                # queue mirror, so a mirror that reads 0 and a completion
                # that reads NULL can never both be observed. The per-field
                # write above deliberately does not stamp it: an item with
                # one field still queued is a partial stage, not a
                # completion.
                conn.execute(
                    "UPDATE workshop_items SET translation_priority = 0, "
                    "translate_version = ?, translated_at = ? WHERE workshop_id = ?",
                    (stamp_value, int(time.time()), item_id)
                )

            conn.commit()

            left_queued = len(batch) - translated_count
            if left_queued:
                logging.info(
                    f"Batch translation: {translated_count} translated, "
                    f"{left_queued} left queued."
                )
            else:
                logging.info(f"Batch translation: {translated_count} fields translated.")

        except Exception as e:
            # Deliberately not swallowed. The caller owns the backoff, and eating
            # the exception here is what let a failing batch be retried every
            # ~1.2 s: the loop could not tell failure from success.
            logging.debug(f"Batch translation error: {e}")
            raise
        finally:
            conn.close()

        return (translated_count, left_queued)


