"""The translator's wire format: boundary phrases, packing by size, partial replies.

The JSON envelope this replaced cost about a quarter of the translation budget.
*Measured* in the live log over the last 300,000 lines (roughly 6.5 hours): 256
batches succeeded, 83 failed, and `No translation returned` was zero. Every
failure was an invalid escape inside the JSON string (`Invalid \\escape`,
`Invalid \\uXXXX escape`) plus one `database is locked`, and each one discarded
the whole request -- 20 fields, billed, re-sent. Asking for boundary blocks
instead means the model never has to escape the translated text, so that failure
mode cannot occur.

The boundary is a phrase of four ordinary words rather than a run of punctuation:
a word costs one token for three to five characters where punctuation costs one
per character, and a nonce -- not the shape of the line -- is what makes a
boundary unambiguous.
"""

import random
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from src.database import (
    queue_field_for_translation,
    get_connection,
    get_next_batch_for_translation,
    initialize_database,
    insert_or_update_item,
)
from src.translator import (
    BATCH_FILL_WAIT_SECONDS,
    DEFAULT_BATCH_CHAR_CAP,
    DEFAULT_BATCH_ITEMS,
    DEFAULT_TEMPERATURE,
    PHRASE_ATTEMPTS,
    PHRASE_WORDS,
    RETRY_BASE_SECONDS,
    TranslationResponseError,
    TranslatorThread,
    boundary_line,
    build_wire_request,
    choose_phrase,
    field_label,
    generate_phrase,
    match_translations,
    pack_batch,
    split_blocks,
    system_prompt,
)
from src.wordlist import WORDS

SOURCE = "テキスト"

# A phrase in the shape the generator produces, used wherever a test needs the
# boundary to be known rather than drawn.
PHRASE = "goat smelt bob and"


# ── fixtures and helpers ─────────────────────────────────────────────────────

def _row(item_id: int, field: str, text: str, priority: int = 3) -> dict:
    return {
        "id": item_id, "item_type": "item", "item_id": item_id,
        "field": field, "original_text": text, "priority": priority,
    }


def _reply(phrase: str, batch: list[dict], english: dict[str, str]) -> str:
    """A reply in the shape the request asks for: boundaries copied, text below."""
    return "\n".join(
        f"{boundary_line(row, phrase)}\n{english[row['field']]}" for row in batch
    )


class _CannedRng:
    """A random source drawing phrases in a fixed order, for collision cases."""

    def __init__(self, *phrases):
        self._phrases = [phrase.split() for phrase in phrases]
        self.draws = 0

    def sample(self, _population, k):
        self.draws += 1
        words = self._phrases.pop(0) if len(self._phrases) > 1 else self._phrases[0]
        assert len(words) == k
        return words


@contextmanager
def _phrase_fixed():
    """Hold the boundary phrase still so a stub reply can be written in advance."""
    with patch("src.translator.choose_phrase", return_value=PHRASE):
        yield


def _thread(db_path, **openai) -> TranslatorThread:
    config = {
        "database": {"path": db_path},
        "openai": {
            "api_key": "SK-TEST", "endpoint": "https://test/v1",
            "model": "gpt-test", **openai,
        },
    }
    thread = TranslatorThread(config)
    thread.db_path = db_path
    return thread


def _client(payload: str):
    """A stub OpenAI client whose single reply is `payload`."""
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = payload
    client.chat.completions.create.return_value = response
    return client


def _capturing_client(payload: str):
    """The same, but recording the kwargs each request was made with."""
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = payload
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        return response

    client.chat.completions.create.side_effect = create
    client.calls = calls
    return client


def _queued(db_path, fields, item_id: int = 1, priority: int = 10) -> list[dict]:
    """Queue `fields` on one item and return the rows the poll would hand over."""
    insert_or_update_item(db_path, {
        "workshop_id": item_id, "title": SOURCE, "status": 200,
    })
    for field in fields:
        queue_field_for_translation(db_path, "item", item_id, field, SOURCE, priority)
    return get_next_batch_for_translation(db_path, limit=len(fields))


def _stored(db_path, column: str, item_id: int = 1):
    conn = get_connection(db_path)
    try:
        return conn.execute(
            f"SELECT {column} FROM workshop_items WHERE workshop_id = ?", (item_id,)
        ).fetchone()[column]
    finally:
        conn.close()


def _queued_count(db_path, item_id: int = 1) -> int:
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM translation_queue WHERE item_id = ?", (item_id,)
        ).fetchone()["n"]
    finally:
        conn.close()


# ── the boundary phrase ──────────────────────────────────────────────────────

def test_a_phrase_is_four_distinct_words_reproducible_under_a_seed():
    phrase = generate_phrase(random.Random(7))
    words = phrase.split()
    assert len(words) == PHRASE_WORDS == 4
    assert len(set(words)) == 4, "four distinct words"
    assert all(word in WORDS for word in words)
    assert phrase == generate_phrase(random.Random(7)), "the same seed, the same phrase"


def test_the_same_phrase_prefixes_every_block_in_one_request():
    batch = [_row(11, "title_en", SOURCE), _row(12, "title_en", SOURCE)]
    request = build_wire_request(batch, PHRASE)
    boundaries = [line for line in request.split("\n") if line.startswith(PHRASE)]
    assert len(boundaries) == 2, "one boundary per field"
    for line in boundaries:
        assert line.startswith(f"{PHRASE} "), "the whole phrase, not a word of it"


def test_the_instructions_name_the_phrase_and_ask_for_it_verbatim():
    """The phrase is ordinary English, so it has to be flagged as copy-me text."""
    prompt = system_prompt(PHRASE)
    assert PHRASE in prompt
    assert "exactly" in prompt
    assert "do not translate" in prompt


def test_a_phrase_that_occurs_in_the_source_text_is_regenerated():
    """A batch containing the phrase would make the boundary ambiguous."""
    phrase_in_text = "acid acorn acre acts"
    other = "zoom zone zero zipper"
    batch = [_row(1, "title_en", f"a title mentioning {phrase_in_text}")]
    rng = _CannedRng(phrase_in_text, other)
    assert choose_phrase(batch, rng) == other
    assert rng.draws == 2


def test_a_phrase_collision_is_bounded_and_never_spins():
    """Every possible draw collides: the budget runs out and the phrase is used."""
    collides = "acid acorn acre acts"
    batch = [_row(1, "title_en", collides)]
    rng = _CannedRng(collides)
    assert choose_phrase(batch, rng) == collides
    assert rng.draws == PHRASE_ATTEMPTS


def test_a_phrase_the_source_does_not_contain_is_taken_first_time():
    batch = [_row(1, "title_en", SOURCE)]
    rng = _CannedRng(PHRASE)
    assert choose_phrase(batch, rng) == PHRASE
    assert rng.draws == 1


# ── field labels ─────────────────────────────────────────────────────────────

def test_field_labels_are_single_word_aliases():
    """One token each: a label is repeated on every block of every request."""
    assert field_label("title_en") == "title"
    assert field_label("short_description_en") == "short"
    assert field_label("extended_description_en") == "long"
    assert field_label("personaname_en") == "user"


def test_an_unmapped_field_falls_back_to_its_own_name():
    """There is no fifth field today; a future one must not break alignment.

    The fallback is the queue's field name, which stays unique per field, so such a
    field still round-trips -- it is simply labelled with a longer word.
    """
    assert field_label("future_field_en") == "future_field_en"


def test_every_alias_round_trips_through_the_wire():
    """The regexes accept the aliases -- confirmed rather than assumed.

    `FIELD_LABEL_RE` is what the tolerant fallback matches, so a label it could not
    read would silently cost a batch its alignment whenever the model mangled the
    phrase. Every alias has to survive write, split and match.
    """
    for field, label in [
        ("title_en", "title"),
        ("short_description_en", "short"),
        ("extended_description_en", "long"),
        ("personaname_en", "user"),
    ]:
        row = _row(4, field, SOURCE)
        assert boundary_line(row, PHRASE) == f"{PHRASE} 4 {label}"
        reply = f"{boundary_line(row, PHRASE)}\nHello"
        assert split_blocks(reply, PHRASE) == [(4, label, "Hello")]
        assert match_translations(reply, [row], PHRASE) == {0: "Hello"}


def test_short_and_extended_descriptions_keep_distinct_labels():
    """Collapsing both to `description` would make a mismatched block unattributable.

    An item can have both queued at once, and the fallback alignment keys on
    `(item_id, field label)`.
    """
    assert field_label("short_description_en") == "short"
    assert field_label("extended_description_en") == "long"
    assert field_label("short_description_en") != field_label("extended_description_en")


def test_both_descriptions_align_independently():
    """An item can have both queued, so their labels have to stay distinct."""
    batch = [_row(1, "short_description_en", SOURCE),
             _row(1, "extended_description_en", SOURCE)]
    reply = (
        f"{PHRASE} 1 long\nThe long one\n"
        f"{PHRASE} 999 title\nNot ours\n"
        f"{PHRASE} 1 short\nThe short one"
    )
    assert match_translations(reply, batch, PHRASE) == {
        0: "The short one", 1: "The long one",
    }


# ── packing: the owner's algorithm ───────────────────────────────────────────

def test_the_first_candidate_is_taken_even_when_it_alone_exceeds_the_cap():
    """The cap is consulted from the second candidate onwards, never the first.

    A field larger than the cap has to go on its own; refusing it would mean the
    longest descriptions could never be translated at all.
    """
    batch, _had_more = pack_batch(
        [_row(1, "extended_description_en", "x" * 5000), _row(2, "title_en", "y")],
        char_cap=4000, item_ceiling=20,
    )
    assert [row["item_id"] for row in batch] == [1]
    assert len(batch[0]["original_text"]) > 4000


def test_the_cap_is_checked_before_the_second_candidate_is_taken():
    batch, had_more = pack_batch(
        [_row(1, "extended_description_en", "x" * 3000),
         _row(2, "extended_description_en", "y" * 2000)],
        char_cap=4000, item_ceiling=20,
    )
    assert [row["item_id"] for row in batch] == [1], "3000 + 2000 exceeds 4000"
    assert had_more is True


def test_a_candidate_that_exactly_reaches_the_cap_is_taken():
    """The cap is a ceiling, not an exclusive bound: 4000 fits in 4000."""
    batch, _ = pack_batch(
        [_row(1, "extended_description_en", "x" * 2000),
         _row(2, "extended_description_en", "y" * 2000)],
        char_cap=4000, item_ceiling=20,
    )
    assert [row["item_id"] for row in batch] == [1, 2]


def test_a_single_candidate_batch_may_exceed_the_cap():
    batch, had_more = pack_batch(
        [_row(1, "extended_description_en", "x" * 7725)], char_cap=4000, item_ceiling=20
    )
    assert len(batch) == 1
    assert had_more is False, "there was no further candidate to refuse"


def test_the_item_ceiling_bounds_a_request():
    """The cap bounds a request's size; the ceiling bounds the rows one can cost."""
    batch, had_more = pack_batch(
        [_row(i, "title_en", "x") for i in range(1, 30)], char_cap=4000, item_ceiling=20
    )
    assert len(batch) == 20
    assert had_more is True, "the ceiling refused the twenty-first candidate"


def test_a_starved_batch_is_reported_as_having_no_more_candidates():
    """The flag is how the loop tells 'full' from 'the queue gave all it had'."""
    batch, had_more = pack_batch(
        [_row(1, "title_en", "x"), _row(2, "title_en", "y")],
        char_cap=4000, item_ceiling=20,
    )
    assert len(batch) == 2
    assert had_more is False


# ── parsing: positional first, boundaries as the fallback ────────────────────

def test_an_exact_reply_round_trips():
    batch = [_row(1, "title_en", SOURCE), _row(2, "short_description_en", SOURCE)]
    reply = _reply(PHRASE, batch, {
        "title_en": "A Tale of Two Cities",
        "short_description_en": "It was the best of times,\nit was the worst of times.",
    })
    assert match_translations(reply, batch, PHRASE) == {
        0: "A Tale of Two Cities",
        1: "It was the best of times,\nit was the worst of times.",
    }


def test_a_reply_missing_a_block_resolves_the_rest_by_boundary():
    """The rows that came back are usable; the one that did not stays queued."""
    batch = [_row(1, "title_en", SOURCE), _row(2, "short_description_en", SOURCE)]
    reply = _reply(PHRASE, batch[:1], {"title_en": "A Tale of Two Cities"})
    assert match_translations(reply, batch, PHRASE) == {0: "A Tale of Two Cities"}


def test_reordered_blocks_align_by_id_and_label_and_extras_are_ignored():
    batch = [_row(1, "title_en", SOURCE), _row(2, "short_description_en", SOURCE)]
    reply = (
        f"{PHRASE} 2 short\nTimes\n"
        f"{PHRASE} 77 long\nNot ours\n"
        f"{PHRASE} 1 title\nCities"
    )
    assert match_translations(reply, batch, PHRASE) == {0: "Cities", 1: "Times"}


def test_a_mangled_phrase_falls_back_to_position():
    """The model wrote the right number of blocks in order and wrecked the phrase.

    Positional assignment is consulted first for exactly this reason, and the
    tolerant boundary pattern is what finds the blocks to assign.
    """
    batch = [_row(1, "title_en", SOURCE), _row(2, "short_description_en", SOURCE)]
    reply = "cat dog fish bird 1 title\nOne\ncat dog fish bird 2 short\nTwo"
    assert match_translations(reply, batch, PHRASE) == {0: "One", 1: "Two"}


def test_an_empty_block_leaves_its_row_unresolved():
    """A boundary with nothing under it is not a translation."""
    batch = [_row(1, "title_en", SOURCE), _row(2, "short_description_en", SOURCE)]
    reply = f"{PHRASE} 1 title\n\n{PHRASE} 2 short\nTimes"
    assert match_translations(reply, batch, PHRASE) == {1: "Times"}


def test_a_boundary_shaped_line_inside_the_text_does_not_split_the_block():
    """The nonce is what makes this safe, and it is why the phrase is not dashes.

    Ordinary prose contains dashes, and can contain a line shaped like a boundary.
    It cannot contain *this request's* four-word phrase -- `choose_phrase` redraws
    when it does, which is pinned by the collision test above -- so a
    boundary-shaped line that is not the nonce stays part of the translation.
    """
    batch = [_row(1, "short_description_en", SOURCE)]
    content = "A Tale\ncat dog fish bird 999 title\nof Two Cities\n----------"
    reply = f"{boundary_line(batch[0], PHRASE)}\n{content}"
    assert match_translations(reply, batch, PHRASE) == {0: content}
    assert split_blocks(reply, PHRASE) == [(1, "short", content)]


def test_a_preamble_before_the_first_boundary_is_ignored():
    batch = [_row(1, "title_en", SOURCE)]
    reply = f"Here are the translations:\n{boundary_line(batch[0], PHRASE)}\nCities"
    assert match_translations(reply, batch, PHRASE) == {0: "Cities"}


def test_a_hyphenated_phrase_word_is_a_usable_boundary():
    """`yo-yo` is in the list, and both writer and reader accept it."""
    phrase = "yo-yo acid acorn acre"
    batch = [_row(5, "title_en", SOURCE)]
    reply = f"{boundary_line(batch[0], phrase)}\nHello"
    assert boundary_line(batch[0], phrase) == f"{phrase} 5 title"
    assert match_translations(reply, batch, phrase) == {0: "Hello"}


# ── the request carries no envelope to get wrong ─────────────────────────────

def test_the_request_is_written_in_the_shape_of_the_reply():
    batch = [_row(11, "title_en", SOURCE), _row(12, "extended_description_en", SOURCE)]
    request = build_wire_request(batch, PHRASE)
    assert f"{PHRASE} 11 title" in request
    assert f"{PHRASE} 12 long" in request
    assert request.index(f"{PHRASE} 11 title") < request.index(
        f"{PHRASE} 12 long"
    ), "queue order is the order the reply is read in"


def test_the_request_contains_no_json_envelope():
    """The whole point: nothing in the request asks the model to escape text."""
    request = build_wire_request([_row(1, "title_en", SOURCE)], PHRASE)
    for artefact in ('{"id"', '"translated"', '"text":', "json", "[{", "}]"):
        assert artefact not in request


def test_no_json_envelope_reaches_the_api_call(db_path):
    batch = _queued(db_path, ["title_en"])
    reply = _reply(PHRASE, batch, {"title_en": "Cities"})
    client = _capturing_client(reply)
    with _phrase_fixed():
        _thread(db_path)._translate_batch(batch, client, "gpt-test")

    sent = client.calls[0]
    user_message = next(m for m in sent["messages"] if m["role"] == "user")
    system_message = next(m for m in sent["messages"] if m["role"] == "system")
    assert PHRASE in user_message["content"]
    assert PHRASE in system_message["content"], "the instructions name the phrase"
    assert '{"id"' not in user_message["content"]


# ── model parameters ─────────────────────────────────────────────────────────

def test_temperature_defaults_to_zero_and_is_configurable(db_path):
    """Translation is not creative; sampling only adds variance."""
    assert _thread(db_path).temperature == DEFAULT_TEMPERATURE == 0.0
    assert _thread(db_path, temperature=0.7).temperature == 0.7
    # Nonsense falls back rather than reaching the API as NaN.
    assert _thread(db_path, temperature="hot").temperature == DEFAULT_TEMPERATURE


def test_the_configured_temperature_reaches_the_request(db_path):
    batch = _queued(db_path, ["title_en"])
    client = _capturing_client(_reply(PHRASE, batch, {"title_en": "Cities"}))
    with _phrase_fixed():
        _thread(db_path, temperature=0.4)._translate_batch(batch, client, "gpt-test")
    assert client.calls[0]["temperature"] == 0.4


def test_max_tokens_is_never_sent(db_path):
    """A translation, not a summary: a truncated translation is a corrupt one."""
    batch = _queued(db_path, ["title_en"])
    client = _capturing_client(_reply(PHRASE, batch, {"title_en": "Cities"}))
    with _phrase_fixed():
        _thread(db_path)._translate_batch(batch, client, "gpt-test")
    assert "max_tokens" not in client.calls[0]


def test_the_packing_keys_default_from_the_measurable_distribution(db_path):
    """The defaults are documented against the queue's own lengths."""
    thread = _thread(db_path)
    assert thread.batch_size == DEFAULT_BATCH_ITEMS == 20
    assert thread.batch_char_cap == DEFAULT_BATCH_CHAR_CAP == 4000
    assert _thread(db_path, batch=5).batch_size == 5
    assert _thread(db_path, batch_char_cap=500).batch_char_cap == 500
    # Unusable values fall back rather than disabling the bounds.
    assert _thread(db_path, batch=0).batch_size == DEFAULT_BATCH_ITEMS
    assert _thread(db_path, batch_char_cap="wide").batch_char_cap == DEFAULT_BATCH_CHAR_CAP


# ── partial success through the real writer ──────────────────────────────────

def test_a_full_reply_stores_every_field_and_clears_the_queue(db_path):
    batch = _queued(db_path, ["title_en", "short_description_en"])
    english = {"title_en": "Hello", "short_description_en": "World"}
    with _phrase_fixed():
        translated, left = _thread(db_path)._translate_batch(
            batch, _client(_reply(PHRASE, batch, english)), "gpt-test"
        )

    assert (translated, left) == (2, 0)
    assert _stored(db_path, "title_en") == "Hello"
    assert _stored(db_path, "short_description_en") == "World"
    assert _queued_count(db_path) == 0
    assert _stored(db_path, "translation_priority") == 0


def test_a_partial_reply_stores_what_arrived_and_leaves_the_rest_queued(db_path):
    """A missing block must not discard the blocks that did arrive."""
    batch = _queued(db_path, ["title_en", "short_description_en"])
    reply = _reply(PHRASE, batch[:1], {"title_en": "Hello"})

    with _phrase_fixed():
        translated, left = _thread(db_path)._translate_batch(
            batch, _client(reply), "gpt-test"
        )

    assert (translated, left) == (1, 1)
    assert _stored(db_path, "title_en") == "Hello"
    assert _stored(db_path, "short_description_en") is None
    assert _queued_count(db_path) == 1, "the unanswered field stays queued for a later pass"
    assert _stored(db_path, "translation_priority") == 10, (
        "a field is still queued, so the item is not complete"
    )


def test_a_reply_with_no_usable_block_raises_a_retryable_error(db_path):
    """Zero blocks is a failure: the rows are all still queued and were billed for.

    Without this the caller would treat an unparseable reply as success and
    re-send the same batch immediately, which is the tight loop the backoff exists
    to stop.
    """
    batch = _queued(db_path, ["title_en"])
    thread = _thread(db_path)

    with pytest.raises(TranslationResponseError):
        thread._translate_batch(batch, _client("I am afraid I cannot do that."), "gpt-test")

    assert _queued_count(db_path) == 1


def test_an_empty_batch_makes_no_request(db_path):
    client = _client("")
    assert _thread(db_path)._translate_batch([], client, "gpt-test") == (0, 0)
    assert not client.chat.completions.create.called


# ── the loop: partial success does not back off, zero blocks does ────────────

def test_a_partial_reply_does_not_grow_the_failure_streak(db_path):
    """One usable block is partial success: no failure, no backoff.

    The rows it missed are still queued, so growing the streak for them would back
    off the work the model did return.
    """
    batch = _queued(db_path, ["title_en", "short_description_en"])
    payload = _reply(PHRASE, batch[:1], {"title_en": "Hello"})
    thread = _thread(db_path)
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        thread.running = False

    with patch("src.translator.choose_phrase", return_value=PHRASE), \
         patch("src.translator.get_next_batch_for_translation", return_value=batch), \
         patch("src.translator._create_openai_client", return_value=_client(payload)), \
         patch.object(TranslatorThread, "_sleep", side_effect=fake_sleep):
        thread.run()

    assert thread._failure_streak == 0
    assert sleeps == [1], "the normal inter-batch pause, not a backoff"
    assert _queued_count(db_path) == 1


def test_a_reply_with_no_usable_block_backs_off(db_path):
    batch = _queued(db_path, ["title_en"])
    thread = _thread(db_path)
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        thread.running = False

    with patch("src.translator.choose_phrase", return_value=PHRASE), \
         patch("src.translator.get_next_batch_for_translation", return_value=batch), \
         patch("src.translator._create_openai_client",
               return_value=_client("Here is a summary instead.")), \
         patch.object(TranslatorThread, "_sleep", side_effect=fake_sleep):
        thread.run()

    assert thread._failure_streak == 1
    assert sleeps == [RETRY_BASE_SECONDS]
    assert _queued_count(db_path) == 1


# ── the loop: wait once for a batch to fill, then send it ────────────────────

def test_an_under_cap_batch_is_sent_after_the_wait_instead_of_re_polled(db_path):
    """The old loop re-polled a partial batch every 30 s and never sent it.

    A single backlog field therefore never translated. This one row is under both
    the cap and the ceiling, and not urgent, so it is exactly the case that used to
    wait forever: it must be sent after one bounded wait.
    """
    batch = _queued(db_path, ["title_en"], priority=1)
    payload = _reply(PHRASE, batch, {"title_en": "Cities"})
    thread = _thread(db_path)
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            thread.running = False

    with patch("src.translator.choose_phrase", return_value=PHRASE), \
         patch("src.translator.get_next_batch_for_translation", return_value=batch), \
         patch("src.translator._create_openai_client", return_value=_client(payload)), \
         patch.object(TranslatorThread, "_sleep", side_effect=fake_sleep):
        thread.run()

    assert sleeps == [BATCH_FILL_WAIT_SECONDS, 1], (
        "one bounded wait, then the send; never a second wait on the same batch"
    )
    assert _stored(db_path, "title_en") == "Cities"
    assert _queued_count(db_path) == 0


def test_an_urgent_field_is_sent_without_waiting(db_path):
    """A detail-view bump must not sit behind the fill deadline."""
    batch = _queued(db_path, ["title_en"], priority=10)
    payload = _reply(PHRASE, batch, {"title_en": "Cities"})
    thread = _thread(db_path)
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        thread.running = False

    with patch("src.translator.choose_phrase", return_value=PHRASE), \
         patch("src.translator.get_next_batch_for_translation", return_value=batch), \
         patch("src.translator._create_openai_client", return_value=_client(payload)), \
         patch.object(TranslatorThread, "_sleep", side_effect=fake_sleep):
        thread.run()

    assert sleeps == [1]
    assert _stored(db_path, "title_en") == "Cities"
