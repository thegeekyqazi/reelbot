"""Allowlist of Discord user IDs, persisted as JSON with atomic writes."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


class Allowlist:
    def __init__(
        self,
        path: Path,
        owner_id: int,
        *,
        allow_everyone: bool = False,
        seed: Iterable[int] = (),
    ) -> None:
        self.path = Path(path)
        self.owner_id = owner_id
        self.allow_everyone = allow_everyone
        self._users: set[int] = set()

        if self.path.exists():
            self._load()
        else:
            self._users = set(seed)
            if self._users:
                self._save()

    def is_allowed(self, user_id: int) -> bool:
        return self.allow_everyone or user_id == self.owner_id or user_id in self._users

    def add(self, user_id: int) -> bool:
        """Add a user. Returns False if they were already on the list."""
        if user_id in self._users:
            return False
        self._users.add(user_id)
        self._save()
        return True

    def remove(self, user_id: int) -> bool:
        """Remove a user. Returns False if they weren't on the list."""
        if user_id not in self._users:
            return False
        self._users.discard(user_id)
        self._save()
        return True

    def users(self) -> list[int]:
        return sorted(self._users)

    def _load(self) -> None:
        with self.path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self._users = {int(u) for u in data.get("users", [])}
        log.info("Loaded %d allowlisted user(s) from %s", len(self._users), self.path)

    def _save(self) -> None:
        """Write to a temp file in the same directory, then rename over the target."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".allowlist-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"users": [str(u) for u in self.users()]}, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
