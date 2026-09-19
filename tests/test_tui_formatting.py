"""TUI formatting helpers: a bad value degrades to "N/A", never raises.

``format_ts``'s handler used a name that was not in scope, so instead of the
documented fallback it raised ``NameError`` out of the render path. These tests
pin the fallback, not the exception message.
"""
import datetime
import logging

from src.tui import format_count, format_size, format_ts


def test_format_ts_returns_na_for_a_string_instead_of_raising():
    """``datetime.fromtimestamp("2026-09-19")`` raises TypeError, so the handler runs."""
    assert format_ts("2026-09-19") == "N/A"


def test_format_ts_returns_na_for_an_out_of_range_timestamp():
    assert format_ts(1e30) == "N/A"


def test_format_ts_returns_na_for_a_missing_timestamp():
    assert format_ts(None) == "N/A"
    assert format_ts(0) == "N/A"


def test_format_ts_formats_a_real_timestamp():
    stamp = int(datetime.datetime(2026, 9, 19, 12, 0).timestamp())
    assert format_ts(stamp) == "2026-09-19"


def test_format_size_returns_na_and_logs_the_value_it_was_given(caplog):
    """The handler logged the ``bytes`` builtin rather than the argument."""
    bad = object()

    with caplog.at_level(logging.DEBUG):
        assert format_size(bad) == "N/A"

    assert repr(bad) in caplog.text
    assert "<class 'bytes'>" not in caplog.text


def test_format_count_reads_zero_as_measured_and_only_the_unknown_as_na():
    """A zero is a measured zero, not missing data.

    ``format_count`` treated every falsy value alike, so ``0`` read "N/A" while
    the web's ``fmtCount(0)`` returns ``'0'`` -- the same count meant two
    different things on the two front ends. Only a missing or unparsable value
    is unknown.
    """
    assert format_count(0) == "[gray]0[/gray]"
    assert format_count(0) != "[gray]N/A[/gray]"

    for unknown in (None, "", "not a number"):
        assert format_count(unknown) == "[gray]N/A[/gray]"
