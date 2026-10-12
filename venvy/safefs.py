"""Path checks and symlink-safe file access inside app directories."""
import contextlib
import os
import secrets
import shutil
import urllib.parse
from pathlib import Path
from typing import List, Optional


def is_safe_path(root: Path, target: Path) -> bool:
    """Return True only if *target* resolves to a path strictly inside (or equal to) *root*."""
    try:
        root_r = root.resolve()
        target_r = target.resolve()
        return target_r == root_r or root_r in target_r.parents
    except (ValueError, OSError):
        return False


# Files Venvy itself writes into an app directory. Users must not be able to
# overwrite or delete them through the file manager.
_PROTECTED_APP_FILE_PREFIXES = (".jailrc", ".jailsh", ".bashrc-venvy")


def _is_protected_app_file(name: str) -> bool:
    return name.startswith(_PROTECTED_APP_FILE_PREFIXES)


def _upload_rel_path_error(rel_path: str) -> Optional[str]:
    """Return why *rel_path* (a browser-supplied relative upload path, possibly
    containing folders) is unacceptable, or None if it's fine. Containment
    inside the app dir is still checked separately with is_safe_path()."""
    if not rel_path or rel_path.startswith("/") or "\\" in rel_path:
        return "invalid path"
    parts = rel_path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return "invalid path"
        if any(ord(c) < 32 or ord(c) == 127 for c in part):
            return "name contains control characters"
        if len(part.encode("utf-8")) > 255:
            return "name is too long"
    if _is_protected_app_file(parts[-1]):
        return "reserved filename"
    return None


# ---------------------------------------------------------------------------
# File access inside app directories
# ---------------------------------------------------------------------------
# With per-app users Venvy is root and the directory belongs to the app, which
# can change it at any moment — including swapping a directory for a symlink
# between Venvy checking a path and using it. A path string that was safe when
# checked is therefore worth nothing by the time it is opened: root would
# follow the new link and write (or read, or chown) wherever it points.
#
# So nothing below uses a path twice. A request path is resolved once (which
# is what lets an app's own, legitimate symlinks keep working), reduced to its
# components under the app directory, and then walked from the app directory
# one component at a time with O_NOFOLLOW. Every operation happens relative to
# the directory fd that walk returns. If anything on the way has turned into a
# symlink since it was resolved, the walk fails instead of following it.

_O_NOFOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


_FS_COPY_BUF = 1024 * 1024


class AppPathError(Exception):
    """A path inside an app directory can't be used as asked. Carries the
    HTTP status the API should answer with."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _app_rel_parts(root: Path, rel_path: str, follow_last: bool = True) -> tuple:
    """Check a client-supplied path against the app directory *root* and
    return its components below it. With follow_last=False the final
    component is taken literally rather than resolved, for operations that
    act on a directory entry itself (delete, rename): deleting a symlink
    should remove the link, not what it points to."""
    root_r = root.resolve()
    cand = root / (rel_path or "")
    if follow_last or cand.name in ("", ".", ".."):
        target = cand.resolve()
    else:
        target = cand.parent.resolve() / cand.name
    if not (target == root_r or root_r in target.parents):
        raise AppPathError(403, "Path is outside the app directory.")
    return target.relative_to(root_r).parts


@contextlib.contextmanager
def _open_app_dir(root: Path, parts: tuple, create: bool = False, owner: Optional[tuple] = None):
    """Yield a directory fd for root/parts, reached without following any
    symlink below root. create=True makes missing directories on the way
    (owned by *owner* when given)."""
    fd = os.open(root.resolve(), _O_NOFOLLOW_DIR)
    try:
        for part in parts:
            try:
                try:
                    nfd = os.open(part, _O_NOFOLLOW_DIR, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise AppPathError(404, f"'{part}' does not exist.")
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, 0o755, dir_fd=fd)
                    nfd = os.open(part, _O_NOFOLLOW_DIR, dir_fd=fd)
                    if owner:
                        os.fchown(nfd, *owner)
            except OSError as exc:
                # ELOOP / ENOTDIR: a symlink or a plain file where a folder
                # was expected.
                raise AppPathError(409, f"'{part}' is not a folder.") from exc
            os.close(fd)
            fd = nfd
        yield fd
    finally:
        os.close(fd)


def _fs_lstat(dir_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _fs_is_dir(st: Optional[os.stat_result]) -> bool:
    import stat as _stat
    return st is not None and _stat.S_ISDIR(st.st_mode)


def _fs_write_new(dir_fd: int, name: str, chunks, mode: int, owner: Optional[tuple], limit: Optional[int] = None) -> bool:
    """Write *chunks* to dir_fd/name via a temp file + rename, so a crash or
    full disk never leaves a truncated file behind. Returns False (nothing
    changed) if the data exceeds *limit* bytes."""
    tmp = f".{name}.venvy-{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        written = 0
        with os.fdopen(fd, "wb") as f:
            for chunk in chunks:
                written += len(chunk)
                if limit is not None and written > limit:
                    return False
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), mode)
            if owner:
                os.fchown(f.fileno(), *owner)
        # rename() replaces a symlink itself, it never writes through one.
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)


def _fs_read_chunks(fileobj):
    while True:
        buf = fileobj.read(_FS_COPY_BUF)
        if not buf:
            return
        yield buf


def _fs_save(root: Path, parts: tuple, data: bytes, owner: Optional[tuple]) -> None:
    """Create or overwrite the file root/parts, keeping an existing file's mode."""
    import stat as _stat
    with _open_app_dir(root, parts[:-1], create=True, owner=owner) as dfd:
        st = _fs_lstat(dfd, parts[-1])
        if _fs_is_dir(st):
            raise AppPathError(409, "That path is a folder.")
        mode = _stat.S_IMODE(st.st_mode) if st is not None and _stat.S_ISREG(st.st_mode) else 0o644
        _fs_write_new(dfd, parts[-1], [data], mode, owner)


def _fs_write_stream(root: Path, parts: tuple, fileobj, limit: int, owner: Optional[tuple]) -> bool:
    """Stream *fileobj* into root/parts. False if it is larger than *limit*."""
    with _open_app_dir(root, parts[:-1], create=True, owner=owner) as dfd:
        if _fs_is_dir(_fs_lstat(dfd, parts[-1])):
            raise AppPathError(409, f"'{parts[-1]}' already exists as a folder.")
        return _fs_write_new(dfd, parts[-1], _fs_read_chunks(fileobj), 0o644, owner, limit)


def _fs_install(root: Path, parts: tuple, src: Path, owner: Optional[tuple]) -> None:
    """Move *src* — a finished file in Venvy's own temp space — to root/parts."""
    with _open_app_dir(root, parts[:-1], create=True, owner=owner) as dfd:
        if _fs_is_dir(_fs_lstat(dfd, parts[-1])):
            raise AppPathError(409, f"'{parts[-1]}' already exists as a folder.")
        if owner:
            os.lchown(src, *owner)
        try:
            os.replace(src, parts[-1], dst_dir_fd=dfd)
        except OSError:
            # Different filesystem (e.g. DATA_DIR/apps is a mount) — copy instead.
            mode = os.stat(src).st_mode & 0o777
            with open(src, "rb") as f:
                _fs_write_new(dfd, parts[-1], _fs_read_chunks(f), mode, owner)
            src.unlink(missing_ok=True)


def _fs_touch(root: Path, parts: tuple, owner: Optional[tuple]) -> None:
    with _open_app_dir(root, parts[:-1], create=True, owner=owner) as dfd:
        try:
            fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC, 0o644, dir_fd=dfd)
        except FileExistsError:
            # Same as touch(1) on an existing entry: just bump its mtime.
            os.utime(parts[-1], dir_fd=dfd, follow_symlinks=False)
            return
        try:
            if owner:
                os.fchown(fd, *owner)
        finally:
            os.close(fd)


def _fs_mkdir(root: Path, parts: tuple, owner: Optional[tuple]) -> None:
    with _open_app_dir(root, parts, create=True, owner=owner):
        pass


def _fs_rmtree(dir_fd: int, name: str) -> None:
    """Remove the directory dir_fd/name and everything in it, best effort,
    descending only into real directories (never through a symlink)."""
    try:
        sub = os.open(name, _O_NOFOLLOW_DIR, dir_fd=dir_fd)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dir_fd)
        return
    try:
        with os.scandir(sub) as it:
            entries = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
        for child, is_dir in entries:
            if is_dir:
                _fs_rmtree(sub, child)
            else:
                with contextlib.suppress(OSError):
                    os.unlink(child, dir_fd=sub)
    finally:
        os.close(sub)
    with contextlib.suppress(OSError):
        os.rmdir(name, dir_fd=dir_fd)


def _fs_delete(root: Path, parts: tuple) -> None:
    with _open_app_dir(root, parts[:-1]) as dfd:
        st = _fs_lstat(dfd, parts[-1])
        if st is None:
            return
        if _fs_is_dir(st):
            _fs_rmtree(dfd, parts[-1])
        else:
            os.unlink(parts[-1], dir_fd=dfd)


def _fs_copy_entry(src_fd: int, src_name: str, dst_fd: int, dst_name: str, owner: Optional[tuple]) -> None:
    """Copy one directory entry (file, symlink or whole directory) between
    two directory fds. Symlinks are copied as symlinks, never followed."""
    import stat as _stat
    st = os.stat(src_name, dir_fd=src_fd, follow_symlinks=False)
    if _stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(src_name, dir_fd=src_fd), dst_name, dir_fd=dst_fd)
        if owner:
            os.chown(dst_name, *owner, dir_fd=dst_fd, follow_symlinks=False)
    elif _stat.S_ISDIR(st.st_mode):
        os.mkdir(dst_name, 0o700, dir_fd=dst_fd)
        s_sub = os.open(src_name, _O_NOFOLLOW_DIR, dir_fd=src_fd)
        try:
            d_sub = os.open(dst_name, _O_NOFOLLOW_DIR, dir_fd=dst_fd)
            try:
                if owner:
                    os.fchown(d_sub, *owner)
                with os.scandir(s_sub) as it:
                    names = [e.name for e in it]
                for name in names:
                    _fs_copy_entry(s_sub, name, d_sub, name, owner)
                os.fchmod(d_sub, _stat.S_IMODE(st.st_mode))
            finally:
                os.close(d_sub)
        finally:
            os.close(s_sub)
    elif _stat.S_ISREG(st.st_mode):
        fd = os.open(src_name, os.O_RDONLY | os.O_NOFOLLOW | _O_CLOEXEC, dir_fd=src_fd)
        with os.fdopen(fd, "rb") as f:
            out = os.open(dst_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC, 0o600, dir_fd=dst_fd)
            with os.fdopen(out, "wb") as o:
                shutil.copyfileobj(f, o, _FS_COPY_BUF)
                os.fchmod(o.fileno(), _stat.S_IMODE(st.st_mode))
                if owner:
                    os.fchown(o.fileno(), *owner)
        os.utime(dst_name, ns=(st.st_atime_ns, st.st_mtime_ns), dir_fd=dst_fd, follow_symlinks=False)


    # Sockets, FIFOs and devices are not copied.


def _fs_move_or_copy(root: Path, src: tuple, dst: tuple, copy: bool, owner: Optional[tuple], dst_label: str) -> None:
    """Rename (copy=False) or copy the entry root/src to root/dst."""
    with _open_app_dir(root, src[:-1]) as sfd, _open_app_dir(root, dst[:-1], create=True, owner=owner) as dfd:
        s_st = _fs_lstat(sfd, src[-1])
        if s_st is None:
            raise AppPathError(404, "Source does not exist.")
        d_st = _fs_lstat(dfd, dst[-1])
        # The one overwrite allowed: renaming an entry onto itself, which is
        # how a case-only rename looks on a case-insensitive filesystem.
        same = d_st is not None and (d_st.st_dev, d_st.st_ino) == (s_st.st_dev, s_st.st_ino)
        if d_st is not None and not (same and not copy):
            raise AppPathError(409, f"'{dst_label}' already exists.")
        if copy:
            _fs_copy_entry(sfd, src[-1], dfd, dst[-1], owner)
        else:
            os.rename(src[-1], dst[-1], src_dir_fd=sfd, dst_dir_fd=dfd)


def _fs_open_file(root: Path, parts: tuple):
    """Open the regular file root/parts for reading; returns (file object,
    stat). Raises AppPathError(404) for anything else."""
    import stat as _stat
    if not parts:
        raise AppPathError(404, "File not found.")
    with _open_app_dir(root, parts[:-1]) as dfd:
        try:
            # O_NONBLOCK: don't hang on a FIFO that appeared in the meantime.
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC, dir_fd=dfd)
        except OSError:
            raise AppPathError(404, "File not found.")
    st = os.fstat(fd)
    if not _stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise AppPathError(404, "File not found.")
    os.set_blocking(fd, True)
    return os.fdopen(fd, "rb"), st


def _fs_listdir(root: Path, parts: tuple) -> List[dict]:
    """[{name, is_dir, size}] for the directory root/parts ([] if it isn't one)."""
    try:
        with _open_app_dir(root, parts) as dfd:
            out = []
            with os.scandir(dfd) as it:
                for entry in it:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        is_dir = False
                    out.append({"name": entry.name, "is_dir": is_dir, "size": size})
            return out
    except AppPathError:
        return []


def _file_download_response(fileobj, st: os.stat_result, filename: str, media_type: Optional[str] = None):
    """What FileResponse(path, filename=...) would send, but from a file that
    is already open — the path is not looked up a second time."""
    import mimetypes
    from starlette.responses import StreamingResponse

    def _iter():
        # Exactly st_size bytes, as promised in Content-Length. A log or a
        # database that grows while it is being sent would otherwise overrun
        # the header, which the HTTP layer treats as a broken response.
        remaining = st.st_size
        with fileobj:
            while remaining > 0:
                buf = fileobj.read(min(_FS_COPY_BUF, remaining))
                if not buf:
                    return
                remaining -= len(buf)
                yield buf

    quoted = urllib.parse.quote(filename)
    disposition = (f'attachment; filename="{filename}"' if quoted == filename
                   else f"attachment; filename*=utf-8''{quoted}")
    return StreamingResponse(
        _iter(),
        media_type=media_type or mimetypes.guess_type(filename)[0] or "text/plain",
        headers={
            "Content-Length": str(st.st_size),
            "Content-Disposition": disposition,
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )
