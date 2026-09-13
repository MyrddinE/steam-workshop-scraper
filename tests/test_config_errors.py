"""A malformed config must be reported, not crash and not be ignored.

`load_config` let the YAML parser's exception escape, and every entry point
caught only `FileNotFoundError`, so a typo in a hand-edited file ended in a raw
`ScannerError` traceback. The usual trigger is an unescaped Windows path, which
is worth naming in the message because the fix is not obvious.
"""

import pytest
import yaml

from src.config import ConfigError, describe_yaml_error, load_config


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_missing_file_still_raises_FileNotFoundError(tmp_path):
    """The absent-file contract is unchanged; callers answer it with defaults."""
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "absent.yaml"))


def test_a_malformed_file_raises_ConfigError(tmp_path):
    path = _write(tmp_path, 'daemon:\n  outbox_dir: "D:\\Temp\\dsh"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_ConfigError_is_not_FileNotFoundError(tmp_path):
    """Callers distinguish the two: one gets defaults, the other gets reported."""
    path = _write(tmp_path, 'daemon:\n  outbox_dir: "D:\\Temp\\dsh"\n')
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert not isinstance(caught.value, FileNotFoundError)


def test_the_message_names_the_file_and_the_position(tmp_path):
    path = _write(tmp_path, 'daemon:\n  outbox_dir: "D:\\Temp\\dsh"\n')
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    message = str(caught.value)
    assert path in message
    assert "line 2" in message
    assert "not valid YAML" in message


def test_the_message_offers_both_fixes_for_a_windows_path(tmp_path):
    path = _write(tmp_path, 'daemon:\n  outbox_dir: "D:\\Temp\\dsh"\n')
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    message = str(caught.value)
    assert "double it" in message
    assert "single quotes" in message


@pytest.mark.parametrize("text", [
    "daemon:\n  outbox_dir: 'D:\\Temp\\dsh'\n",     # single quotes: literal
    'daemon:\n  outbox_dir: "D:\\\\Temp\\\\dsh"\n',  # doubled in double quotes
    "daemon:\n  outbox_dir: D:\\Temp\\dsh\n",        # unquoted: literal
])
def test_the_working_forms_all_load(tmp_path, text):
    config = load_config(_write(tmp_path, text))
    assert config["daemon"]["outbox_dir"] == "D:\\Temp\\dsh"


def test_describe_yaml_error_survives_a_mark_less_error():
    """Some YAML errors carry no position; the message must still be usable."""
    class Bare(yaml.YAMLError):
        problem = "something went wrong"

    assert "something went wrong" in describe_yaml_error("config.yaml", Bare())
