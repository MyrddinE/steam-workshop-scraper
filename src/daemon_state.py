"""Runtime state the daemon has to survive a restart with.

A backoff is not configuration. The delay a worker has reached, and the moment
its next attempt is due, describe what the daemon is *currently doing* about a
condition that will pass — not a setting anyone chose. Keeping them in
``config.yaml`` would mean a restart could not tell "the operator lowered this
deliberately" from "the backoff was mid-flight", and would put a value nobody
edits into the file people edit.

It is a file rather than a table for the same reason: the database holds the
library, the queues and the work, and its schema is versioned and migrated.
Transient pacing state has no place in that history — the ``app_state`` table
that used to exist was dropped as obsolete (``src/database.py``), and this
module deliberately does not revive it.

The file sits beside the database, following the convention already set by the
TUI's ``.tui_state.yaml``, and is plain YAML so an operator can read the current
backoff without a client.

Every operation is best-effort. A state file that cannot be read is treated as
absent and a state file that cannot be written is logged and ignored: losing the
memory of a backoff costs one extra attempt after a restart, while letting a
diagnostic exception escape would stop the work it exists to pace. This is the
same rule the failure capture follows.
"""

import logging
import os
import threading

import yaml

# Beside the database, named for what writes it.
DEFAULT_STATE_NAME = ".daemon_state.yaml"


def state_path_for(db_path: str) -> str:
    """The state file that belongs to ``db_path``'s installation."""
    directory = os.path.dirname(os.path.abspath(db_path))
    return os.path.join(directory, DEFAULT_STATE_NAME)


class StateStore:
    """A small YAML document of daemon-owned sections, written atomically.

    Sections are top-level keys, so one writer's entry cannot clobber another's:
    :meth:`save` merges into what is on disk rather than replacing the document.
    """

    def __init__(self, path: str):
        self.path = path
        # Guards the read-modify-write in save(). One writer is expected; the
        # lock is here so that adding a second one cannot silently lose an entry.
        self._lock = threading.Lock()

    def load(self) -> dict:
        """Return the stored document, or an empty dict if there is not one.

        A missing file is the normal first-run case, so it is not even a debug
        message. A file that exists but cannot be read or parsed is worth a
        warning: it means state was expected and is being ignored.
        """
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logging.warning("Ignoring unreadable daemon state file %s: %s", self.path, exc)
            return {}
        if not isinstance(data, dict):
            logging.warning(
                "Ignoring daemon state file %s: expected a mapping, found %s",
                self.path, type(data).__name__,
            )
            return {}
        return data

    def save(self, sections: dict) -> bool:
        """Merge ``sections`` into the file. Returns whether it was written.

        Never raises. A failed write is reported because the caller asked for
        the state to be durable and it is not.
        """
        with self._lock:
            document = dict(self.load())
            document.update(sections)
            return self._write(document)

    def remove(self, *keys: str) -> bool:
        """Drop top-level ``keys``, leaving every other section alone."""
        with self._lock:
            document = dict(self.load())
            if not any(key in document for key in keys):
                return True
            for key in keys:
                document.pop(key, None)
            return self._write(document)

    def _write(self, document: dict) -> bool:
        temp_path = self.path + ".tmp"
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(document, handle, default_flow_style=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            # Same-directory temp + os.replace is what makes the update atomic,
            # so a reader never sees a half-written document.
            os.replace(temp_path, self.path)
            return True
        except Exception as exc:
            logging.warning("Could not write daemon state file %s: %s", self.path, exc)
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                # A leftover temp file is harmless: the next write truncates it,
                # and it is never read.
                pass
            return False
