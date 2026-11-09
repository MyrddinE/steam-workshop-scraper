"""The one sign-in-window cooldown shared by the TUI and the web UI."""

import os

from src import signin


def test_one_claim_is_granted_then_the_next_is_refused(tmp_path):
    db_path = str(tmp_path / "workshop.db")

    assert signin.claim_open(db_path, now=1000) is True
    assert signin.claim_open(db_path, now=1001) is False
    assert signin.cooldown_remaining(db_path, now=1001) == \
        signin.COOLDOWN_SECONDS - 1


def test_the_claim_is_granted_again_after_the_cooldown(tmp_path):
    db_path = str(tmp_path / "workshop.db")
    assert signin.claim_open(db_path, now=1000) is True

    assert signin.claim_open(
        db_path, now=1000 + signin.COOLDOWN_SECONDS + 1) is True


def test_a_manual_open_records_the_cooldown_for_the_other_front_end(tmp_path):
    db_path = str(tmp_path / "workshop.db")
    signin.record_open(db_path, now=1000)

    assert signin.claim_open(db_path, now=1001) is False
    assert signin.opened_at(db_path) == 1000


def test_a_missing_or_corrupt_stamp_does_not_block_the_first_open(tmp_path):
    db_path = str(tmp_path / "workshop.db")
    assert signin.claim_open(db_path, now=1000) is True

    other = str(tmp_path / "other.db")
    with open(signin.stamp_path(other), "w", encoding="utf-8") as handle:
        handle.write("not-a-number")
    assert signin.claim_open(other, now=1000) is True


def test_the_stamp_lives_beside_the_database(tmp_path):
    db_path = str(tmp_path / "workshop.db")
    signin.claim_open(db_path, now=1000)

    assert signin.stamp_path(db_path) == os.path.join(
        str(tmp_path), signin.STAMP_NAME)
    assert os.path.exists(signin.stamp_path(db_path))
    assert not os.path.exists(signin.stamp_path(db_path) + signin.LOCK_SUFFIX)
