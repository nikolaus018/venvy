"""Language versions, installed side by side and chosen per app.

The host's own packages (apt's nodejs, cargo, ...) are what an app gets when
it asks for nothing. An app that names a version — in Deploy → Build and run, or in its
repository (.nvmrc, .node-version, package.json "engines", rust-toolchain(.toml),
.python-version, .go-version, .bun-version, .dvmrc) — runs with that version
from TOOLCHAINS_DIR, which Venvy downloads on first use:

    node    nodejs.org, checked against SHASUMS256.txt
    go      go.dev, checked against the sha256 in its release list
    bun     GitHub releases of oven-sh/bun, checked against SHASUMS256.txt
    deno    GitHub releases of denoland/deno, checked against the .sha256sum file
    rust    rustup (itself checked against its .sha256), toolchains through rustup
    python  uv ("uv python install"), when uv is on the host

TOOLCHAINS_DIR is owned by Venvy and readable by every app user; nothing an
app runs can write to it. The download locations can be pointed elsewhere
(VENVY_<KIND>_DIST) for mirrors and tests.
"""
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional

import aiohttp

from venvy.config import DATA_DIR, HTTP_CLIENT_LIMITS, _toml

KINDS = ("node", "go", "bun", "deno", "rust", "python")


def _default_dir() -> Path:
    env = os.getenv("VENVY_TOOLCHAINS_DIR")
    if env:
        return Path(env)
    opt = Path("/opt/venvy")
    if os.geteuid() == 0 and opt.is_dir():
        return opt / "toolchains"
    return DATA_DIR / "toolchains"


TOOLCHAINS_DIR = _default_dir()
AUTO_INSTALL = os.getenv("VENVY_AUTO_TOOLCHAINS", "1") == "1"

DIST = {
    "node": os.getenv("VENVY_NODE_DIST", "https://nodejs.org/dist"),
    "go": os.getenv("VENVY_GO_DIST", "https://go.dev/dl"),
    "bun": os.getenv("VENVY_BUN_DIST", "https://github.com/oven-sh/bun/releases/download"),
    "deno": os.getenv("VENVY_DENO_DIST", "https://github.com/denoland/deno/releases/download"),
    "rustup": os.getenv("VENVY_RUSTUP_DIST", "https://static.rust-lang.org/rustup/dist"),
}

_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.\-+]{0,40}")
_locks: Dict[str, asyncio.Lock] = {}


class ToolchainError(Exception):
    pass


def _arch() -> str:
    m = platform.machine().lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)


def _rust_triple() -> str:
    return {"x64": "x86_64-unknown-linux-gnu", "arm64": "aarch64-unknown-linux-gnu"}.get(_arch(), "")


def clean_version(kind: str, raw: Optional[str]) -> Optional[str]:
    v = (raw or "").strip()
    if v.startswith("v") and kind in ("node", "bun", "deno"):
        v = v[1:]
    if kind == "go" and v.startswith("go"):
        v = v[2:]
    if not v:
        return None
    if not _VERSION_RE.fullmatch(v):
        raise ValueError(f"'{raw}' is not a version.")
    return v


def _vkey(v: str):
    out = []
    for part in re.split(r"[.\-+]", v):
        out.append((0, int(part)) if part.isdigit() else (1, part))
    return out


def _matches(have: str, want: str) -> bool:
    return want in ("latest", "lts", "stable") or have == want or have.startswith(want + ".")


# ---------------------------------------------------------------------------
# What is installed
# ---------------------------------------------------------------------------

def _kind_dir(kind: str) -> Path:
    return TOOLCHAINS_DIR / kind


def installed(kind: str) -> List[str]:
    d = _kind_dir(kind)
    if kind == "rust":
        tc = d / "rustup" / "toolchains"
        if not tc.is_dir():
            return []
        trip = "-" + _rust_triple()
        return sorted((p.name[:-len(trip)] if p.name.endswith(trip) else p.name) for p in tc.iterdir() if p.is_dir())
    if kind == "python":
        if not d.is_dir():
            return []
        out = []
        for p in d.iterdir():
            m = re.match(r"cpython-(\d+\.\d+\.\d+)", p.name)
            if m and p.is_dir():
                out.append(m.group(1))
        return sorted(set(out), key=_vkey)
    if not d.is_dir():
        return []
    return sorted((p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")), key=_vkey)


def find(kind: str, want: str) -> Optional[str]:
    """The newest installed version that satisfies *want*."""
    have = [v for v in installed(kind) if _matches(v, want)]
    if kind == "rust":
        return want if want in installed(kind) else None
    return max(have, key=_vkey) if have else None


def bin_dir(kind: str, version: str) -> Optional[Path]:
    d = _kind_dir(kind)
    if kind == "node":
        p = d / version / "bin"
    elif kind == "go":
        p = d / version / "go" / "bin"
    elif kind in ("bun", "deno"):
        p = d / version
    elif kind == "rust":
        p = d / "rustup" / "toolchains" / f"{version}-{_rust_triple()}" / "bin"
    elif kind == "python":
        cands = sorted(d.glob(f"cpython-{version}*"), key=lambda x: _vkey(x.name.split("-")[1]))
        p = cands[-1] / "bin" if cands else d / "__none__"
    else:
        return None
    return p if p.is_dir() else None


def python_binary(version: str) -> Optional[str]:
    """A Python from the toolchains folder for *version* ("3.13"), if installed."""
    v = find("python", version)
    if not v:
        return None
    b = bin_dir("python", v)
    if not b:
        return None
    for name in (f"python{'.'.join(version.split('.')[:2])}", "python3"):
        if (b / name).exists():
            return str(b / name)
    return None


def describe() -> List[dict]:
    return [{"kind": k, "installed": installed(k), "can_install": _can_install(k)} for k in KINDS]


def _can_install(kind: str) -> bool:
    if os.name != "posix" or not platform.system() == "Linux":
        return bool(os.getenv("VENVY_TOOLCHAINS_ANY_OS"))
    if kind == "python":
        return bool(shutil.which("uv"))
    return True


# ---------------------------------------------------------------------------
# What an app wants
# ---------------------------------------------------------------------------

RUNTIME_KIND = {"node": "node", "static": "node", "bun": "bun", "deno": "deno", "go": "go",
                "rust": "rust", "python": "python"}


def _read(path: Path, limit: int = 100_000) -> Optional[str]:
    try:
        if path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        pass
    return None


def repo_version(kind: str, wd: Path) -> Optional[str]:
    """The version a repository asks for, in the files its ecosystem uses."""
    def first_line(name: str) -> Optional[str]:
        t = _read(wd / name)
        if t is None:
            return None
        for line in t.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                return line
        return None

    try:
        if kind == "node":
            v = first_line(".nvmrc") or first_line(".node-version")
            if not v:
                pkg = _read(wd / "package.json")
                if pkg:
                    with contextlib.suppress(ValueError, AttributeError, TypeError):
                        engines = (json.loads(pkg).get("engines") or {}).get("node") or ""
                        m = re.search(r"(\d+)(?:\.(\d+))?", str(engines))
                        if m:
                            v = m.group(1)
            if v and v.lower().startswith("lts"):
                return None
            return clean_version("node", v) if v else None
        if kind == "rust":
            t = _read(wd / "rust-toolchain.toml")
            if t and _toml is not None:
                with contextlib.suppress(Exception):
                    ch = (_toml.loads(t).get("toolchain") or {}).get("channel")
                    if ch:
                        return clean_version("rust", str(ch))
            v = first_line("rust-toolchain")
            return clean_version("rust", v) if v else None
        if kind == "python":
            v = first_line(".python-version")
            return clean_version("python", v) if v else None
        if kind == "go":
            v = first_line(".go-version")
            return clean_version("go", v) if v else None
        if kind == "bun":
            v = first_line(".bun-version")
            return clean_version("bun", v) if v else None
        if kind == "deno":
            v = first_line(".dvmrc")
            return clean_version("deno", v) if v else None
    except ValueError:
        return None
    return None


def wanted(app, wd: Path) -> Optional[tuple]:
    """(kind, version) the app should run with, or None for the host's own."""
    kind = RUNTIME_KIND.get(app.runtime)
    if not kind:
        return None
    v = (app.runtime_version or "").strip() or repo_version(kind, wd)
    if not v:
        return None
    if kind == "python":
        # A Python the host has as python3.X needs no toolchain.
        if shutil.which(f"python{'.'.join(v.split('.')[:2])}"):
            return None
    return kind, v


def path_dirs(app, wd: Path) -> List[str]:
    """Folders to put first on PATH for the app (empty: use the host's tools)."""
    w = wanted(app, wd)
    if not w:
        return []
    kind, v = w
    if kind == "python":
        return []          # the virtualenv is built with it; nothing to add
    have = find(kind, v)
    if not have:
        return []
    b = bin_dir(kind, have)
    return [str(b)] if b else []


def missing(app, wd: Path) -> Optional[tuple]:
    w = wanted(app, wd)
    if not w:
        return None
    kind, v = w
    if kind == "python":
        return None if python_binary(v) else w
    return None if find(kind, v) else w


def tool_env(app, wd: Path) -> Dict[str, str]:
    w = wanted(app, wd)
    env: Dict[str, str] = {}
    if w and w[0] == "rust" and find("rust", w[1]):
        env["RUSTUP_HOME"] = str(_kind_dir("rust") / "rustup")
        env["RUSTUP_TOOLCHAIN"] = w[1]
    if w and w[0] == "go":
        env["GOTOOLCHAIN"] = "local"
    return env


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

def _http() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800, sock_connect=20, sock_read=120),
                                 **HTTP_CLIENT_LIMITS)


async def _get_json(http, url: str):
    async with http.get(url) as r:
        if r.status != 200:
            raise ToolchainError(f"{url} answered HTTP {r.status}.")
        return await r.json(content_type=None)


async def _get_text(http, url: str) -> str:
    async with http.get(url) as r:
        if r.status != 200:
            raise ToolchainError(f"{url} answered HTTP {r.status}.")
        return await r.text()


async def _download(http, url: str, dest: Path, sha256: str, log: Callable[[str], None]) -> None:
    log(f"Downloading {url}")
    h = hashlib.sha256()
    async with http.get(url) as r:
        if r.status != 200:
            raise ToolchainError(f"{url} answered HTTP {r.status}.")
        with open(dest, "wb") as f:
            async for chunk in r.content.iter_chunked(1 << 20):
                h.update(chunk)
                f.write(chunk)
    if h.hexdigest() != sha256.lower():
        raise ToolchainError(f"Checksum mismatch for {url} — not installed.")
    log("Checksum verified.")


def _sum_from(text: str, filename: str) -> str:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0]
    raise ToolchainError(f"No checksum for {filename}.")


def _within(root: str, path: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _checked_members(tf: tarfile.TarFile, dest: Path) -> List[tarfile.TarInfo]:
    """The members of *tf*, refusing any that would land (or point) outside
    *dest*, and devices — what filter="data" does, for Pythons without it."""
    root = os.path.realpath(dest)
    members = []
    for m in tf.getmembers():
        if not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
            raise ToolchainError(f"Unsafe entry in archive: {m.name}")
        target = os.path.realpath(os.path.join(root, m.name))
        if os.path.isabs(m.name) or not _within(root, target):
            raise ToolchainError(f"Unsafe path in archive: {m.name}")
        if m.issym() or m.islnk():
            base = os.path.dirname(target) if m.issym() else root
            link = os.path.realpath(os.path.join(base, m.linkname))
            if os.path.isabs(m.linkname) or not _within(root, link):
                raise ToolchainError(f"Unsafe link in archive: {m.name}")
        m.mode &= 0o755
        members.append(m)
    return members


def _safe_extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:                      # Python without extraction filters (3.11.0 – 3.11.3)
            tf.extractall(dest, members=_checked_members(tf, dest))


def _safe_extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep) and target != dest.resolve():
                raise ToolchainError(f"Unsafe path in archive: {name}")
        zf.extractall(dest)
        for info in zf.infolist():
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                with contextlib.suppress(OSError):
                    os.chmod(dest / info.filename, mode)


def _publish(staging: Path, final: Path) -> None:
    """Move a finished install into place, readable by everyone."""
    for root, dirs, files in os.walk(staging):
        for n in dirs:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(root, n), 0o755)
        for n in files:
            p = os.path.join(root, n)
            with contextlib.suppress(OSError):
                if not os.path.islink(p):
                    st = os.stat(p)
                    os.chmod(p, 0o755 if st.st_mode & 0o111 else 0o644)
    if final.exists():
        shutil.rmtree(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, final)


def _prepare_dirs() -> None:
    TOOLCHAINS_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        TOOLCHAINS_DIR.chmod(0o755)


async def install(kind: str, want: str, log: Optional[Callable[[str], None]] = None) -> str:
    """Install the newest version of *kind* matching *want*; returns the version."""
    log = log or (lambda m: logging.info("toolchain: %s", m))
    if kind not in KINDS:
        raise ToolchainError(f"Unknown toolchain '{kind}'.")
    want = clean_version(kind, want) or "latest"
    if not _can_install(kind):
        raise ToolchainError("python versions need uv on the host (installer: --with-uv)." if kind == "python"
                             else "Toolchains can only be installed on Linux.")
    lock = _locks.setdefault(kind, asyncio.Lock())
    async with lock:
        have = find(kind, want) if want not in ("latest", "lts", "stable") else None
        if have:
            return have
        _prepare_dirs()
        _kind_dir(kind).mkdir(exist_ok=True)
        with contextlib.suppress(OSError):
            _kind_dir(kind).chmod(0o755)
        work = Path(tempfile.mkdtemp(prefix=f".{kind}-", dir=str(_kind_dir(kind))))
        try:
            fn = {"node": _install_node, "go": _install_go, "bun": _install_bun, "deno": _install_deno,
                  "rust": _install_rust, "python": _install_python}[kind]
            return await fn(want, work, log)
        finally:
            shutil.rmtree(work, ignore_errors=True)


async def _install_node(want: str, work: Path, log) -> str:
    arch = _arch()
    async with _http() as http:
        index = await _get_json(http, f"{DIST['node']}/index.json")
        cands = []
        for rel in index:
            v = str(rel.get("version", "")).lstrip("v")
            if want == "lts" and not rel.get("lts"):
                continue
            if _matches(v, want) and f"linux-{arch}" in (rel.get("files") or []):
                cands.append(v)
        if not cands:
            raise ToolchainError(f"No Node.js release matches '{want}' for linux-{arch}.")
        v = max(cands, key=_vkey)
        name = f"node-v{v}-linux-{arch}.tar.xz"
        sums = await _get_text(http, f"{DIST['node']}/v{v}/SHASUMS256.txt")
        archive = work / name
        await _download(http, f"{DIST['node']}/v{v}/{name}", archive, _sum_from(sums, name), log)
    out = work / "x"
    out.mkdir()
    await asyncio.to_thread(_safe_extract_tar, archive, out)
    inner = out / f"node-v{v}-linux-{arch}"
    await asyncio.to_thread(_publish, inner, _kind_dir("node") / v)
    log(f"Node.js {v} installed.")
    return v


async def _install_go(want: str, work: Path, log) -> str:
    arch = {"x64": "amd64", "arm64": "arm64"}.get(_arch(), _arch())
    async with _http() as http:
        rels = await _get_json(http, f"{DIST['go']}/?mode=json&include=all")
        best = None
        for rel in rels:
            v = str(rel.get("version", ""))[2:]
            if not rel.get("stable", True) and want in ("latest", "stable"):
                continue
            if not _matches(v, want):
                continue
            for f in rel.get("files") or []:
                if f.get("os") == "linux" and f.get("arch") == arch and f.get("kind") == "archive":
                    if best is None or _vkey(v) > _vkey(best[0]):
                        best = (v, f["filename"], f["sha256"])
        if not best:
            raise ToolchainError(f"No Go release matches '{want}' for linux-{arch}.")
        v, filename, sha = best
        archive = work / filename
        await _download(http, f"{DIST['go']}/{filename}", archive, sha, log)
    out = work / "x"
    out.mkdir()
    await asyncio.to_thread(_safe_extract_tar, archive, out)
    await asyncio.to_thread(_publish, out, _kind_dir("go") / v)
    log(f"Go {v} installed.")
    return v


async def _github_latest(http, repo: str) -> str:
    data = await _get_json(http, f"https://api.github.com/repos/{repo}/releases/latest")
    return str(data.get("tag_name", ""))


async def _install_bun(want: str, work: Path, log) -> str:
    arch = {"x64": "x64", "arm64": "aarch64"}.get(_arch(), _arch())
    async with _http() as http:
        if want in ("latest", "stable"):
            tag = await _github_latest(http, "oven-sh/bun")
            v = tag.replace("bun-v", "")
        else:
            v = want
        name = f"bun-linux-{arch}.zip"
        base = f"{DIST['bun']}/bun-v{v}"
        sums = await _get_text(http, f"{base}/SHASUMS256.txt")
        archive = work / name
        await _download(http, f"{base}/{name}", archive, _sum_from(sums, name), log)
    out = work / "x"
    out.mkdir()
    await asyncio.to_thread(_safe_extract_zip, archive, out)
    await asyncio.to_thread(_publish, out / f"bun-linux-{arch}", _kind_dir("bun") / v)
    log(f"Bun {v} installed.")
    return v


async def _install_deno(want: str, work: Path, log) -> str:
    arch = {"x64": "x86_64", "arm64": "aarch64"}.get(_arch(), _arch())
    async with _http() as http:
        if want in ("latest", "stable"):
            v = (await _github_latest(http, "denoland/deno")).lstrip("v")
        else:
            v = want
        name = f"deno-{arch}-unknown-linux-gnu.zip"
        base = f"{DIST['deno']}/v{v}"
        sums = await _get_text(http, f"{base}/{name}.sha256sum")
        archive = work / name
        await _download(http, f"{base}/{name}", archive, sums.split()[0], log)
    out = work / "x"
    out.mkdir()
    await asyncio.to_thread(_safe_extract_zip, archive, out)
    await asyncio.to_thread(_publish, out, _kind_dir("deno") / v)
    log(f"Deno {v} installed.")
    return v


async def _run(argv: List[str], env: Dict[str, str], log, timeout: int = 1800) -> None:
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                env=env, cwd="/")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise ToolchainError(f"{Path(argv[0]).name} timed out.")
    text = out.decode(errors="replace").strip()
    if text:
        log(text[-3000:])
    if proc.returncode != 0:
        raise ToolchainError(f"{Path(argv[0]).name} failed (exit {proc.returncode}).")


async def _install_rust(want: str, work: Path, log) -> str:
    trip = _rust_triple()
    if not trip:
        raise ToolchainError(f"Rust toolchains are not supported on {platform.machine()}.")
    rust_dir = _kind_dir("rust")
    rustup_home, cargo_home = rust_dir / "rustup", rust_dir / "cargo"
    rustup = cargo_home / "bin" / "rustup"
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(rust_dir), "RUSTUP_HOME": str(rustup_home),
           "CARGO_HOME": str(cargo_home)}
    channel = "stable" if want == "latest" else want
    if not rustup.exists():
        async with _http() as http:
            url = f"{DIST['rustup']}/{trip}/rustup-init"
            sha = (await _get_text(http, url + ".sha256")).split()[0]
            init = work / "rustup-init"
            await _download(http, url, init, sha, log)
        init.chmod(0o755)
        await _run([str(init), "-y", "--no-modify-path", "--profile", "minimal", "--default-toolchain", "none"], env, log)
    await _run([str(rustup), "toolchain", "install", channel, "--profile", "minimal"], env, log)
    await asyncio.to_thread(_open_up, rust_dir)
    log(f"Rust {channel} installed.")
    return channel


def _open_up(root: Path) -> None:
    for r, dirs, files in os.walk(root):
        for n in dirs:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(r, n), 0o755)
        for n in files:
            p = os.path.join(r, n)
            with contextlib.suppress(OSError):
                if not os.path.islink(p):
                    st = os.stat(p)
                    os.chmod(p, 0o755 if st.st_mode & 0o111 else 0o644)


async def _install_python(want: str, work: Path, log) -> str:
    uv = shutil.which("uv")
    if not uv:
        raise ToolchainError("uv is not installed on this host.")
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin:" + str(Path(uv).parent), "HOME": str(work),
           "UV_PYTHON_INSTALL_DIR": str(_kind_dir("python")), "UV_CACHE_DIR": str(work / "cache")}
    await _run([uv, "python", "install", want if want not in ("latest", "stable") else "3"], env, log)
    await asyncio.to_thread(_open_up, _kind_dir("python"))
    v = find("python", want if want not in ("latest", "stable") else "3")
    if not v:
        raise ToolchainError(f"uv did not install a Python for '{want}'.")
    log(f"Python {v} installed.")
    return v


def remove(kind: str, version: str) -> bool:
    if kind not in KINDS:
        return False
    version = clean_version(kind, version) or ""
    if kind == "rust":
        p = _kind_dir("rust") / "rustup" / "toolchains" / f"{version}-{_rust_triple()}"
    elif kind == "python":
        cands = list(_kind_dir("python").glob(f"cpython-{version}*"))
        for c in cands:
            shutil.rmtree(c, ignore_errors=True)
        return bool(cands)
    else:
        p = _kind_dir(kind) / version
    root = os.path.normpath(str(_kind_dir(kind)))
    target = os.path.normpath(str(p))
    if not target.startswith(root + os.sep):
        return False
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target, ignore_errors=True)
        return True
    return False


async def ensure_for_app(app, wd: Path, log: Callable[[str], None]) -> None:
    """Install the version an app asks for, if it is not there yet."""
    m = missing(app, wd)
    if not m:
        return
    kind, v = m
    if not AUTO_INSTALL:
        raise ToolchainError(f"{kind} {v} is not installed (System → Toolchains), and automatic installs are off.")
    log(f"{kind} {v} is not installed yet — installing it…")
    await install(kind, v, log)
