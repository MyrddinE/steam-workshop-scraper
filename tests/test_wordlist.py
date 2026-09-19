"""The embedded word list is a copy of a published list, so it must not rot.

`src/wordlist.py` holds EFF Short Wordlist #1 in full; the translator draws
four-word boundary phrases from it. A silently truncated or edited list would
still work -- it would just make boundary phrases weaker and less verifiable --
so the size, uniqueness and shape are pinned here.
"""

import re

from src import wordlist
from src.wordlist import WORDS


def test_the_list_is_the_published_eff_short_wordlist_1():
    assert isinstance(WORDS, tuple), "immutable, and no import-time work"
    assert len(WORDS) == 1296, "EFF Short Wordlist #1 has 1296 entries"
    assert len(set(WORDS)) == 1296, "no duplicates"


def test_every_word_is_a_single_short_lower_case_token():
    for word in WORDS:
        assert 3 <= len(word) <= 5, f"{word!r} is not 3-5 characters"
        assert word == word.lower(), f"{word!r} is not lower case"
        assert re.fullmatch(r"[a-z]+(?:-[a-z]+)*", word), f"{word!r} is not a word"


def test_the_one_hyphenated_word_is_kept_and_accounted_for():
    """`yo-yo` is the list's only hyphen; it is kept rather than filtered.

    Keeping it makes the module a faithful copy of the published list, and an
    internal hyphen neither splits a boundary line nor confuses the reader -- the
    writer escapes the phrase and the tolerant pattern allows the hyphen.
    """
    hyphenated = [word for word in WORDS if "-" in word]
    assert hyphenated == ["yo-yo"]


def test_the_attribution_travels_with_the_list():
    """CC BY 3.0 requires attribution, and this is where it lives."""
    doc = wordlist.__doc__ or ""
    assert "EFF Short Wordlist #1" in doc
    assert "Bonneau" in doc
    assert "Electronic Frontier Foundation" in doc
    assert "CC BY 3.0" in doc
    assert "eff.org" in doc
