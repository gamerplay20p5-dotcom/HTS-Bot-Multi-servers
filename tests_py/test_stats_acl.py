"""Simulate Linux openat/ACL permissions on every development platform."""
import os
from pathlib import Path
from uuid import uuid4

import pytest

from manguadu.stats import files
from manguadu.tenant_scope import GuildScope, use_scope


class TraversalOS:
    O_RDONLY = 0
    O_DIRECTORY = 1 << 16
    O_NOFOLLOW = 1 << 17
    O_NONBLOCK = 1 << 18

    def __init__(self, *, path_supported=True, traversal_only=True, readable=True):
        if path_supported:
            self.O_PATH = 1 << 21
        self.supports_dir_fd = {self.open}
        self.traversal_only, self.readable = traversal_only, readable
        self.directories, self.calls = {}, []
        self.getenv, self.fdopen, self.fstat = os.getenv, os.fdopen, os.fstat

    def open(self, path, flags, *, dir_fd=None):
        resolved = self.directories[dir_fd] / path if dir_fd is not None else Path(path)
        self.calls.append((resolved, flags, dir_fd))
        assert flags & self.O_NOFOLLOW
        if flags & self.O_DIRECTORY:
            if self.traversal_only and not flags & getattr(self, "O_PATH", 0):
                raise PermissionError("directory has traversal permission but no listing permission")
            fd = -len(self.calls)
            self.directories[fd] = resolved
            return fd
        assert not flags & getattr(self, "O_PATH", 0), "final file must be opened for reading"
        if not self.readable:
            raise PermissionError("file read denied")
        return os.open(resolved, os.O_RDONLY | getattr(os, "O_BINARY", 0))

    def close(self, fd):
        del self.directories[fd]


@pytest.fixture
def confined_file(tmp_path, monkeypatch):
    uuid = str(uuid4())
    root = tmp_path / uuid / ".cache/Lua/ttf_stats_mp"
    root.mkdir(parents=True)
    path = root / "character.txt"
    path.write_bytes(b"version=1\n")
    monkeypatch.setenv("PTERO_VOLUMES_ROOT", str(tmp_path))
    return GuildScope(1, server={}, environment={"PTERO_VOLUME_ID": uuid}), root, path


def test_o_path_traverses_acl_directories_without_requesting_listing(confined_file, monkeypatch):
    scope, root, path = confined_file
    backend = TraversalOS()
    monkeypatch.setattr(files, "os", backend)
    before = path.stat()
    with use_scope(scope), files.confined_reader(root, path) as stream:
        assert stream.read() == b"version=1\n"
    directories = [flags for _, flags, _ in backend.calls if flags & backend.O_DIRECTORY]
    assert len(directories) == 5
    assert all(flags & backend.O_PATH and flags & backend.O_NOFOLLOW for flags in directories)
    assert not backend.directories
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_without_o_path_readable_directories_keep_no_follow_fallback(confined_file, monkeypatch):
    scope, root, path = confined_file
    backend = TraversalOS(path_supported=False, traversal_only=False)
    monkeypatch.setattr(files, "os", backend)
    with use_scope(scope), files.confined_reader(root, path) as stream:
        assert stream.read() == b"version=1\n"
    assert len(backend.calls) == 6
    assert all(flags & backend.O_NOFOLLOW for _, flags, _ in backend.calls)
    assert not backend.directories


def test_without_o_path_traversal_only_fails_without_weakening_security(confined_file, monkeypatch):
    scope, root, path = confined_file
    backend = TraversalOS(path_supported=False)
    monkeypatch.setattr(files, "os", backend)
    with use_scope(scope), pytest.raises(PermissionError, match="traversal"):
        with files.confined_reader(root, path):
            pytest.fail("must not bypass descriptor confinement")
    assert len(backend.calls) == 1
    assert not backend.directories


def test_o_path_does_not_bypass_final_file_permissions_and_closes_descriptors(confined_file, monkeypatch):
    scope, root, path = confined_file
    backend = TraversalOS(readable=False)
    monkeypatch.setattr(files, "os", backend)
    with use_scope(scope), pytest.raises(PermissionError, match="file read denied"):
        with files.confined_reader(root, path):
            pytest.fail("file must remain unreadable")
    assert not backend.directories


def test_o_path_still_rejects_foreign_volume_before_open(confined_file, monkeypatch):
    _, root, path = confined_file
    backend = TraversalOS()
    monkeypatch.setattr(files, "os", backend)
    with use_scope(GuildScope(2, server={}, environment={"PTERO_VOLUME_ID": str(uuid4())})), pytest.raises(ValueError, match="unsafe"):
        with files.confined_reader(root, path):
            pytest.fail("foreign volume")
    assert backend.calls == []


def test_o_path_still_rejects_symlinks_before_open(confined_file, monkeypatch):
    scope, root, path = confined_file
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda item: item == root.parent or original(item))
    backend = TraversalOS()
    monkeypatch.setattr(files, "os", backend)
    with use_scope(scope), pytest.raises(ValueError, match="unsafe"):
        with files.confined_reader(root, path):
            pytest.fail("symbolic link")
    assert backend.calls == []
