import json
import os

import pytest

from reelbot.allowlist import Allowlist

OWNER = 111


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data" / "allowlist.json"


def test_owner_always_allowed(path):
    al = Allowlist(path, OWNER)
    assert al.is_allowed(OWNER)
    assert not al.is_allowed(222)
    assert al.users() == []


def test_owner_allowed_even_if_removed(path):
    al = Allowlist(path, OWNER)
    al.remove(OWNER)
    assert al.is_allowed(OWNER)


def test_add_remove(path):
    al = Allowlist(path, OWNER)
    assert al.add(222) is True
    assert al.add(222) is False
    assert al.is_allowed(222)
    assert al.remove(222) is True
    assert al.remove(222) is False
    assert not al.is_allowed(222)


def test_persist_and_reload(path):
    al = Allowlist(path, OWNER)
    al.add(333)
    al.add(222)
    reloaded = Allowlist(path, OWNER)
    assert reloaded.users() == [222, 333]
    assert json.loads(path.read_text()) == {"users": ["222", "333"]}

    reloaded.remove(333)
    assert Allowlist(path, OWNER).users() == [222]


def test_seed_only_on_first_run(path):
    al = Allowlist(path, OWNER, seed=[444, 555])
    assert al.users() == [444, 555]
    al.remove(444)
    # Seed must not re-add a user the owner removed.
    assert Allowlist(path, OWNER, seed=[444, 555]).users() == [555]


def test_no_file_written_without_changes(path):
    Allowlist(path, OWNER)
    assert not path.exists()


def test_allow_everyone_override(path):
    al = Allowlist(path, OWNER, allow_everyone=True)
    assert al.is_allowed(999)
    assert Allowlist(path, OWNER, allow_everyone=False).is_allowed(999) is False


def test_atomic_write_keeps_old_file_on_failure(path, monkeypatch):
    al = Allowlist(path, OWNER)
    al.add(222)
    before = path.read_text()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        al.add(333)

    assert path.read_text() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["allowlist.json"]


def test_atomic_write_uses_rename(path, monkeypatch):
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    Allowlist(path, OWNER).add(222)
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == str(path)
    assert os.path.dirname(src) == str(path.parent)
