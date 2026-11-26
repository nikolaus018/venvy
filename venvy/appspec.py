"""How an app is laid out and described, beyond its runtime.

* Its working folder: the app directory, or a folder inside it (``root_dir``)
  for a repository that holds more than one project.
* Its state, outside the code: ``app-state/<owner>/`` with ``home``, ``cache``
  (package and build caches — never in the app folder, so they are not served,
  snapshotted or counted twice), ``data`` (persistent files that survive
  deploys, rollbacks and atomic switches: $APP_DATA_DIR) and ``secrets``.
  Workers and replicas share the state of the app they belong to (the owner).
* Commands the user types (start, install, release, health): parsed here.
* The files a repository can describe itself with: ``venvy.toml`` and a
  ``Procfile``.
* Guessing the runtime from the files an app has.

Nothing here starts a process.
"""
import json
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from venvy.config import DATA_DIR, _toml

STATE_DIR = DATA_DIR / "app-state"

# A relative path inside an app: plain components, no "..", no leading "-".
_COMPONENT_RE = re.compile(r"[A-Za-z0-9_.@+\-][A-Za-z0-9_.@+\-]*")
MAX_COMMAND = 4000

# Shared build caches are faster, but under per-app users one app could
# poison what another one builds from — so they are only offered without
# isolation, and only when asked for.
SHARED_CACHE = os.getenv("VENVY_SHARED_BUILD_CACHE", "0") == "1"


def owner_id(app) -> int:
    """The app whose user, folder and state a process uses."""
    return int(app.parent_app_id or app.id)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def clean_rel_path(raw: Optional[str], what: str = "path", allow_empty: bool = True) -> str:
    """A path relative to the app's folder, normalised ("./a/b/" -> "a/b").
    Raises ValueError for anything that could leave it."""
    p = (raw or "").strip().replace("\\", "/")
    if p.startswith("/"):
        raise ValueError(f"The {what} must be a path inside the app's folder, not an absolute path.")
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/") if p not in ("", ".") else ""
    if not p:
        if allow_empty:
            return ""
        raise ValueError(f"The {what} must not be empty.")
    if len(p) > 400:
        raise ValueError(f"The {what} is too long.")
    parts = p.split("/")
    for part in parts:
        if part in ("", ".", "..") or not _COMPONENT_RE.fullmatch(part) or part.startswith("-"):
            raise ValueError(f"The {what} must be a path inside the app's folder (like src/app.py), "
                             f"without '..'.")
    return "/".join(parts)


def _inside(root: Path, cand: Path) -> bool:
    try:
        real_root = root.resolve()
        real = cand.resolve()
    except OSError:
        return False
    return real == real_root or real_root in real.parents


def work_dir(app) -> Path:
    """The folder the app runs, installs and builds in."""
    base = Path(app.app_dir or ".")
    sub = (getattr(app, "root_dir", "") or "").strip("/")
    if not sub:
        return base
    cand = base / sub
    if cand.is_symlink() or not _inside(base, cand):
        return base / "__invalid_root_dir__"      # never escapes; makes the start fail plainly
    return cand


def work_dir_problem(app) -> Optional[str]:
    sub = (getattr(app, "root_dir", "") or "").strip("/")
    if not sub:
        return None
    wd = work_dir(app)
    if not wd.is_dir():
        return f"The app folder '{sub}' does not exist inside the app. Upload it or change the Root folder setting."
    return None


def state_root(app) -> Path:
    return STATE_DIR / str(owner_id(app))


def home_dir(app) -> Path:
    return state_root(app) / "home"


def cache_dir(app) -> Path:
    if SHARED_CACHE:
        return DATA_DIR / "build-cache"
    return state_root(app) / "cache"


def data_dir(app) -> Path:
    return state_root(app) / "data"


def secrets_dir(app) -> Path:
    return state_root(app) / "secrets" / str(app.id)


def ensure_state_dirs(app, ids: Optional[Tuple[int, int]]) -> None:
    """Create the app's state folders.

    The state root and the secrets folders belong to Venvy (root): the app
    can't rename or replace them, so nothing Venvy writes there as root can be
    redirected with a symlink. home, cache and data belong to the app's user."""
    for d in (STATE_DIR, state_root(app), state_root(app) / "secrets"):
        if d.is_symlink():
            d.unlink()
        d.mkdir(mode=0o711, exist_ok=True)
        with _suppress():
            if ids:
                os.chown(d, 0, 0, follow_symlinks=False)
            d.chmod(0o711)
    owned = [home_dir(app), state_root(app) / "cache", data_dir(app)]
    for d in owned:
        if d.is_symlink():
            d.unlink()
        d.mkdir(mode=0o700, exist_ok=True)
        if ids:
            with _suppress():
                os.chown(d, ids[0], ids[1], follow_symlinks=False)
        with _suppress():
            d.chmod(0o700)
    sd = secrets_dir(app)
    if sd.is_symlink():
        sd.unlink()
    sd.mkdir(mode=0o750, exist_ok=True)
    with _suppress():
        if ids:
            os.chown(sd, 0, ids[1], follow_symlinks=False)
        sd.chmod(0o750 if ids else 0o700)
    if SHARED_CACHE:
        c = cache_dir(app)
        c.mkdir(mode=0o755, exist_ok=True)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return exc[0] is not None and issubclass(exc[0], OSError)


def remove_state(app_id: int) -> None:
    shutil.rmtree(STATE_DIR / str(app_id), ignore_errors=True)


def cache_env(app) -> Dict[str, str]:
    """Where package managers and compilers keep their caches for this app."""
    c = cache_dir(app)
    env = {
        "XDG_CACHE_HOME": str(c / "xdg"),
        "CARGO_HOME": str(c / "cargo"),
        "GOPATH": str(c / "go"),
        "GOMODCACHE": str(c / "go" / "pkg" / "mod"),
        "GOCACHE": str(c / "go-build"),
        "npm_config_cache": str(c / "npm"),
        "BUN_INSTALL_CACHE_DIR": str(c / "bun"),
        "DENO_DIR": str(c / "deno"),
        "PIP_CACHE_DIR": str(c / "pip"),
        "UV_CACHE_DIR": str(c / "uv"),
        "POETRY_CACHE_DIR": str(c / "poetry"),
        "COMPOSER_HOME": str(c / "composer"),
        "GRADLE_USER_HOME": str(c / "gradle"),
        "MAVEN_OPTS": f"-Dmaven.repo.local={c / 'm2'}",
        "COREPACK_HOME": str(c / "corepack"),
        "PNPM_HOME": str(c / "pnpm"),
        "YARN_CACHE_FOLDER": str(c / "yarn"),
        "GEM_SPEC_CACHE": str(c / "gem-specs"),
        "SCCACHE_DIR": str(c / "sccache"),
    }
    if shutil.which("sccache"):
        env["RUSTC_WRAPPER"] = "sccache"
    return env


def app_paths_env(app) -> Dict[str, str]:
    """Variables every process of the app gets, telling it where its things are."""
    env = {
        "APP_DATA_DIR": str(data_dir(app)),
        "SECRETS_DIR": str(secrets_dir(app)),
        "VENVY_APP_ID": str(app.id),
        "VENVY_APP_NAME": str(app.name),
        "VENVY_ENVIRONMENT": str(getattr(app, "environment", "") or "production"),
    }
    if getattr(app, "process_name", None):
        env["VENVY_PROCESS"] = str(app.process_name)
    # A private app is told to listen on loopback only (HOST and BIND_HOST,
    # which many frameworks read). A public one gets neither: what it binds is
    # its own business, and HOST must never look like Venvy's own setting.
    if not getattr(app, "bind_public", True):
        env["HOST"] = env["BIND_HOST"] = "127.0.0.1"
    return env


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def clean_command(raw: Optional[str], what: str = "command") -> Optional[str]:
    """A command as stored: None when blank. Raises ValueError if it can't be parsed."""
    c = (raw or "").strip()
    if not c:
        return None
    if len(c) > MAX_COMMAND:
        raise ValueError(f"The {what} is longer than {MAX_COMMAND} characters.")
    if "\0" in c or "\n" in c or "\r" in c:
        raise ValueError(f"The {what} must be one line.")
    try:
        if not shlex.split(c):
            return None
    except ValueError as exc:
        raise ValueError(f"The {what} can't be parsed: {exc}.")
    return c


_VAR_RE = re.compile(r"\$(?:(\$)|\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")


def expand(word: str, env: Dict[str, str]) -> str:
    """$NAME, ${NAME} and ${NAME:-default} from *env*, $$ for a literal $;
    nothing else (no shell)."""
    def sub(m):
        if m.group(1):
            return "$"
        name = m.group(2) or m.group(4)
        val = env.get(name)
        if not val and m.group(3) is not None:
            return m.group(3)
        return val or ""
    return _VAR_RE.sub(sub, word)


def command_argv(cmd: str, env: Dict[str, str], cwd: Optional[Path] = None) -> List[str]:
    """The argv of a start command. A first word naming a file in the app's
    folder ("./server", "bin/app") is run from there."""
    words = shlex.split(cmd)
    argv = [expand(w, env) for w in words]
    if not argv or not argv[0]:
        raise ValueError("The start command is empty.")
    first = argv[0]
    if cwd is not None and "/" in first and not first.startswith("/"):
        argv[0] = str((cwd / first).resolve()) if _inside(cwd, cwd / first) else first
    return argv


def shell_argv(cmd: str) -> List[str]:
    """Install, release and health commands run through sh, as the app's user."""
    return ["sh", "-c", cmd]


# ---------------------------------------------------------------------------
# Procfile and venvy.toml
# ---------------------------------------------------------------------------

_PROC_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,30}")


def parse_procfile(text: str) -> Dict[str, str]:
    """{process name: command}. Lines that aren't 'name: command' are ignored."""
    out: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        name, _, cmd = line.partition(":")
        name, cmd = name.strip(), cmd.strip()
        if _PROC_NAME_RE.fullmatch(name) and cmd:
            try:
                c = clean_command(cmd, f"Procfile command for {name}")
            except ValueError:
                continue
            if c:
                out[name] = c
    return out


# Keys venvy.toml may set, and the App field each one goes to.
SPEC_KEYS = {
    "runtime": "runtime", "version": "runtime_version", "root": "root_dir", "entrypoint": "entrypoint",
    "start": "start_command", "install": "install_command", "build": "build_command",
    "release": "release_command", "stop_signal": "stop_signal", "stop_timeout": "stop_timeout",
    "instances": "instances", "zero_downtime": "zero_downtime", "port": "container_port",
}
_HEALTH_KEYS = {"path": "healthcheck_path", "type": "health_type", "command": "health_command",
                "interval": "health_interval", "failures": "health_failures", "start_period": "health_start_period"}


class SpecError(ValueError):
    pass


def parse_spec(text: str) -> dict:
    """venvy.toml -> {"fields": {App field: value}, "processes": {name: cmd}, "env": {...}}.

    Example::

        runtime = "rust"
        start = "./target/release/api --port $PORT"
        build = "cargo build --release"
        release = "./target/release/api migrate"
        instances = 2

        [health]
        path = "/healthz"

        [processes]
        worker = "./target/release/api worker"

        [env]
        RUST_LOG = "info"
    """
    if _toml is None:
        raise SpecError("venvy.toml needs Python 3.11 or newer on the server.")
    try:
        data = _toml.loads(text or "")
    except Exception as exc:
        raise SpecError(f"venvy.toml is not valid TOML: {exc}")
    fields: Dict[str, object] = {}
    for key, value in data.items():
        if key in SPEC_KEYS:
            fields[SPEC_KEYS[key]] = value
        elif key == "health":
            if not isinstance(value, dict):
                raise SpecError("[health] must be a table.")
            for hk, hv in value.items():
                if hk not in _HEALTH_KEYS:
                    raise SpecError(f"Unknown key health.{hk} in venvy.toml.")
                fields[_HEALTH_KEYS[hk]] = hv
        elif key in ("processes", "env", "keep"):
            continue
        else:
            raise SpecError(f"Unknown key '{key}' in venvy.toml.")
    procs = data.get("processes") or {}
    env = data.get("env") or {}
    if not isinstance(procs, dict) or not all(isinstance(v, str) for v in procs.values()):
        raise SpecError("[processes] must map names to command strings.")
    if not isinstance(env, dict):
        raise SpecError("[env] must be a table.")
    for name in procs:
        if not _PROC_NAME_RE.fullmatch(name) or name == "web":
            raise SpecError(f"Process name '{name}' is not allowed (letters, digits, - and _; not 'web').")
    keep = data.get("keep") or []
    if not isinstance(keep, list) or not all(isinstance(k, str) for k in keep):
        raise SpecError("keep must be a list of file names or patterns.")
    for k in keep:
        if k.startswith("/") or ".." in k.split("/"):
            raise SpecError(f"keep: '{k}' must be inside the app.")
    return {"fields": fields, "processes": {k: clean_command(v, f"process {k}") for k, v in procs.items()},
            "env": {str(k): str(v) for k, v in env.items()}, "keep": keep}


def read_repo_spec(wd: Path) -> Tuple[Optional[dict], Dict[str, str]]:
    """(parsed venvy.toml or None, Procfile processes) from a working folder."""
    spec = None
    procs: Dict[str, str] = {}
    toml_file = wd / "venvy.toml"
    if toml_file.is_file() and not toml_file.is_symlink():
        spec = parse_spec(toml_file.read_text(encoding="utf-8", errors="replace")[:200_000])
    proc_file = wd / "Procfile"
    if proc_file.is_file() and not proc_file.is_symlink():
        procs = parse_procfile(proc_file.read_text(encoding="utf-8", errors="replace")[:100_000])
    return spec, procs


# ---------------------------------------------------------------------------
# Guessing the runtime
# ---------------------------------------------------------------------------

def detect_runtime(wd: Path) -> Tuple[Optional[str], str, str]:
    """(runtime, entrypoint, why) for the files in *wd*; (None, "", why) if nothing fits."""
    def has(*names: str) -> Optional[str]:
        for n in names:
            if (wd / n).is_file():
                return n
        return None

    if has("venvy.toml"):
        try:
            spec, _ = read_repo_spec(wd)
            rt = (spec or {}).get("fields", {}).get("runtime")
            if isinstance(rt, str) and rt:
                return rt, str((spec or {})["fields"].get("entrypoint") or ""), "venvy.toml names it"
        except SpecError:
            pass
    if has("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
        return "compose", has("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml") or "", "a compose file"
    if has("Cargo.toml"):
        return "rust", "", "Cargo.toml"
    if has("go.mod"):
        cmd = wd / "cmd"
        if not (wd / "main.go").is_file() and cmd.is_dir():
            subs = sorted(p.name for p in cmd.iterdir() if p.is_dir())
            if len(subs) == 1:
                return "go", f"cmd/{subs[0]}", "go.mod with one command in cmd/"
        return "go", "", "go.mod"
    if has("deno.json", "deno.jsonc"):
        return "deno", "", "deno.json"
    if has("bun.lockb", "bun.lock"):
        return "bun", "", "a Bun lockfile"
    if has("package.json"):
        try:
            pkg = json.loads((wd / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pkg = {}
        scripts = pkg.get("scripts") if isinstance(pkg, dict) else {}
        scripts = scripts if isinstance(scripts, dict) else {}
        if "start" not in scripts and "build" in scripts and not pkg.get("main"):
            return "static", "", "package.json that only builds (a static site)"
        return "node", "", "package.json"
    if has("pom.xml", "build.gradle", "build.gradle.kts"):
        return "java", "", "a Maven or Gradle build"
    jars = sorted(wd.glob("*.jar"))
    if jars:
        return "java", jars[0].name, "a JAR file"
    if has("composer.json", "index.php"):
        return "php", "", "PHP files"
    if has("Gemfile", "config.ru"):
        return "ruby", "config.ru" if has("config.ru") else "app.rb", "Ruby files"
    if has("requirements.txt", "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile"):
        for n in ("main.py", "app.py", "server.py", "manage.py", "run.py", "wsgi.py"):
            if has(n):
                return "python", n, "Python project"
        return "python", "", "Python project"
    for n in ("main.py", "app.py", "server.py"):
        if has(n):
            return "python", n, "a Python file"
    if has("Dockerfile", "Containerfile"):
        return "docker", "", "a Dockerfile"
    if has("index.html"):
        return "static", "", "index.html"
    for sub in ("public", "dist", "build", "site", "www"):
        if (wd / sub / "index.html").is_file():
            return "static", sub, f"{sub}/index.html"
    return None, "", "nothing recognisable"
