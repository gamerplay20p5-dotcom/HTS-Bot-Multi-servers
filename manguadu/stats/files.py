"""Bounded, no-symlink reads. POSIX directory descriptors prevent path races."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path

from ..tenant_scope import current_scope
from .discovery import safe_child


@contextmanager
def confined_reader(root: Path, path: Path):
    if not safe_child(root, path):
        raise ValueError("unsafe path")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("not a regular file")
    if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
        scope = current_scope()
        anchor = Path(os.getenv("PTERO_VOLUMES_ROOT", "/var/lib/pterodactyl/volumes")).resolve() if scope else root
        relative = path.relative_to(anchor)
        # Linux O_PATH needs traversal permission only; O_RDONLY would also
        # require directory-listing access on every intermediate Wings folder.
        # Other POSIX systems retain the confined O_RDONLY/no-follow fallback.
        directory_flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(anchor, directory_flags)
        try:
            for part in relative.parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_fd
            file_fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(file_fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("not a regular file")
                yield stream
        finally:
            os.close(descriptor)
    else:
        # Windows development fallback: resolve and reject reparse/symlink paths.
        with path.open("rb") as stream:
            if not safe_child(root, path):
                raise ValueError("unsafe path")
            yield stream
