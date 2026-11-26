"""The runtimes Venvy can host, and everything that differs between them.

One table, `RUNTIMES`; the API, the process launcher, the startup checks and
the UI (through GET /api/runtimes) all read it. Adding a runtime means adding
an entry here, its branches in the few `if key == ...` helpers below, and the
host packages in install.sh.

Nothing in this module starts a process: it only says *what* to run. The
callers run it (as the app's user, under the app's systemd scope), so every
command is an argv list — no shell is ever involved, and a package name is
validated before it gets anywhere near one.

Python's virtualenv and pip are the one runtime whose set-up is not described
here (it needs the uploaded requirements.txt); the API handles that itself.
"""
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from venvy.config import ASSETS_DIR

_ASSET_DIR = ASSETS_DIR / "venvy" / "runtime_assets"


# How an entrypoint is treated.
REQUIRED = "required"    # must be set before the app can start (main.py)
OPTIONAL = "optional"    # may be blank: the runtime then has a default way to start
FIXED = "fixed"          # always the same, cannot be edited (main.rs)

# What the Packages tab offers.
PKG_MANAGE = "manage"    # list, install, remove
PKG_LIST = "list"        # list only
PKG_NONE = "none"        # nothing to manage


@dataclass(frozen=True)
class Runtime:
    key: str
    label: str
    tools: Tuple[str, ...]            # executables the host needs to run the app
    install_hint: str                 # how to get them
    entry: str                        # REQUIRED | OPTIONAL | FIXED
    entry_default: str                # shown for FIXED; suggested for REQUIRED
    entry_label: str
    entry_placeholder: str
    entry_help: str
    packages: str = PKG_NONE
    pkg_tools: Tuple[str, ...] = ()   # extra executables the package manager needs
    pkg_help: str = ""                # what the Packages tab says when there is nothing to manage
    pkg_example: str = ""             # placeholder of the install box


@dataclass(frozen=True)
class Step:
    """One command to run for an app, as its user."""
    argv: List[str]
    timeout: int
    what: str
    in_app_dir: bool = False          # run from the app's (working) directory instead of "/"
    as_venvy: bool = False            # run as Venvy itself (docker: the app's user has no daemon access)
    env: Tuple[Tuple[str, str], ...] = ()   # extra variables for this step


RUNTIMES: Dict[str, Runtime] = {r.key: r for r in (
    Runtime(
        key="python", label="Python 3 (Venv)",
        tools=("python3",),
        install_hint="sudo apt-get install -y python3 python3-venv",
        entry=REQUIRED, entry_default="main.py", entry_label="Entrypoint File",
        entry_placeholder="main.py",
        entry_help="The Python file that launches your app (e.g. main.py). Upload files first if the app has none yet.",
        packages=PKG_MANAGE, pkg_example="Package name, e.g. numpy==1.26",
    ),
    Runtime(
        key="node", label="Node.js (npm)",
        tools=("node",), pkg_tools=("npm",),
        install_hint="sudo apt-get install -y nodejs npm   (or re-run the installer with --with-node)",
        entry=OPTIONAL, entry_default="", entry_label="Entrypoint File",
        entry_placeholder="server.js (blank = npm start)",
        entry_help="The file that launches your app (e.g. server.js). Leave it blank to run the \"start\" script of package.json (npm start).",
        packages=PKG_MANAGE, pkg_example="Package name, e.g. express@4",
    ),
    Runtime(
        key="bun", label="Bun",
        tools=("bun",),
        install_hint="re-run the installer with --with-bun   (https://bun.sh)",
        entry=OPTIONAL, entry_default="", entry_label="Entrypoint File",
        entry_placeholder="index.ts (blank = bun run start)",
        entry_help="The file that launches your app (e.g. index.ts). Leave it blank to run the \"start\" script of package.json.",
        packages=PKG_MANAGE, pkg_example="Package name, e.g. hono@4",
    ),
    Runtime(
        key="deno", label="Deno",
        tools=("deno",),
        install_hint="re-run the installer with --with-deno   (https://deno.com)",
        entry=OPTIONAL, entry_default="", entry_label="Entrypoint File",
        entry_placeholder="main.ts (blank = deno task start)",
        entry_help="The file that launches your app (e.g. main.ts), run with all permissions. Leave it blank to run the \"start\" task of deno.json.",
        packages=PKG_MANAGE, pkg_example="Package, e.g. jsr:@std/path or npm:express",
    ),
    Runtime(
        key="rust", label="Rust (Cargo)",
        tools=("cargo",),
        install_hint="re-run the installer with --with-rust, or pick a version in Settings (installed with rustup)",
        entry=OPTIONAL, entry_default="", entry_label="Binary",
        entry_placeholder="blank = the package's binary",
        entry_help="Rust apps are built with cargo build --release and run from target/release/. Name the binary when the crate or workspace has several (cargo build --bin NAME).",
        packages=PKG_MANAGE, pkg_example="Crate, e.g. serde@1",
    ),
    Runtime(
        key="go", label="Go",
        tools=("go",),
        install_hint="re-run the installer with --with-go, or pick a version in Settings",
        entry=OPTIONAL, entry_default="", entry_label="Package",
        entry_placeholder="blank = . (or e.g. ./cmd/server)",
        entry_help="Go apps are built with go build into .venvy/bin/app and run from there. Name the main package if it is not the app's root folder (e.g. ./cmd/server).",
        packages=PKG_MANAGE, pkg_example="Module, e.g. github.com/gin-gonic/gin@v1.10.0",
    ),
    Runtime(
        key="php", label="PHP",
        tools=("php",), pkg_tools=("composer",),
        install_hint="sudo apt-get install -y php-cli composer unzip   (or re-run the installer with --with-php)",
        entry=OPTIONAL, entry_default="", entry_label="Router Script",
        entry_placeholder="blank = serve the folder",
        entry_help="Leave blank to serve the app's folder (or its public/ folder) with PHP's built-in web server; unknown paths go to index.php. Or name a router script (e.g. index.php) to handle every request yourself.",
        packages=PKG_MANAGE, pkg_example="Package, e.g. monolog/monolog:^3",
    ),
    Runtime(
        key="ruby", label="Ruby (Bundler)",
        tools=("ruby",), pkg_tools=("bundle",),
        install_hint="sudo apt-get install -y ruby ruby-bundler build-essential   (or re-run the installer with --with-ruby)",
        entry=REQUIRED, entry_default="app.rb", entry_label="Entrypoint File",
        entry_placeholder="app.rb or config.ru",
        entry_help="The Ruby file that launches your app (e.g. app.rb). A config.ru is started with rackup. With a Gemfile the app runs under bundle exec.",
        packages=PKG_MANAGE, pkg_example="Gem name, e.g. sinatra@4.0",
    ),
    Runtime(
        key="java", label="Java (JAR, Maven, Gradle)",
        tools=("java",),
        install_hint="sudo apt-get install -y default-jre-headless   (or re-run the installer with --with-java; Maven/Gradle builds also need maven or a wrapper)",
        entry=OPTIONAL, entry_default="", entry_label="JAR File",
        entry_placeholder="blank = the JAR the build made (target/, build/libs/)",
        entry_help="The executable JAR, run with java -jar (a path like target/app.jar is fine). With a pom.xml or build.gradle the app is built first (mvnw/mvn package, gradlew/gradle build). JVM options go in JAVA_TOOL_OPTIONS, or write your own start command. The port is passed as PORT and SERVER_PORT.",
        pkg_help="Java dependencies come from the build (pom.xml / build.gradle); they are listed here, and installed by the build.",
        packages=PKG_LIST,
    ),
    Runtime(
        key="static", label="Static site",
        tools=("python3",),
        install_hint="sudo apt-get install -y python3",
        entry=OPTIONAL, entry_default="", entry_label="Site Folder",
        entry_placeholder="blank = public, dist, build, site or the app folder",
        entry_help="The folder that holds the site. Blank picks the first of public, dist, build, site, www or html that exists, else the app's own folder. index.html is the home page; /about serves about.html; a 404.html is used for unknown paths.",
        pkg_help="A static site has no packages. With a package.json that has a build script, the site is built with it first.",
    ),
    Runtime(
        key="command", label="Any command / binary",
        tools=(),
        install_hint="",
        entry=OPTIONAL, entry_default="", entry_label="Executable",
        entry_placeholder="e.g. ./server or bin/app (or set a start command)",
        entry_help="Runs a program you upload or build — a compiled binary, a script, a server you downloaded. Name the executable (a path inside the app), or write the whole command line in Start command. $PORT and the app's other variables are expanded.",
        pkg_help="Nothing is managed for a plain command; put an install command in Settings if it needs one.",
    ),
    Runtime(
        key="docker", label="Docker image / Dockerfile",
        tools=("docker",),
        install_hint="re-run the installer with --with-docker   (or install Docker Engine)",
        entry=OPTIONAL, entry_default="", entry_label="Image",
        entry_placeholder="e.g. ghcr.io/org/app:1.2 (blank = build the Dockerfile)",
        entry_help="An image to run, or blank to build the Dockerfile in the app's folder. The container's port (Container port in Settings, default 8080) is published on the app's port on this host; $APP_DATA_DIR is mounted at /data. Docker apps run through the Docker daemon, which is root on this host.",
        pkg_help="Dependencies are part of the image.",
    ),
    Runtime(
        key="compose", label="Docker Compose",
        tools=("docker",),
        install_hint="re-run the installer with --with-docker   (Docker Engine with the compose plugin)",
        entry=OPTIONAL, entry_default="", entry_label="Compose File",
        entry_placeholder="blank = compose.yaml / docker-compose.yml",
        entry_help="Runs docker compose up for the app's folder as one project. $PORT (the app's port) and the app's variables can be used in the compose file. Publish ports on 127.0.0.1 to keep them behind Venvy.",
        pkg_help="Dependencies are part of the images.",
    ),
)}

RUNTIME_KEYS: Tuple[str, ...] = tuple(RUNTIMES)
RUNTIME_ERROR = "runtime must be one of: " + ", ".join(RUNTIME_KEYS)

# Runtimes whose processes run as root through the Docker daemon from files
# the app controls (a compose file can ask for anything the daemon can do):
# only an admin may give an app one of these. compose_guard checks every file
# anyway; docker apps are run by Venvy's own fixed docker run line instead.
ROOT_RUNTIMES = ("compose",)
ROOT_RUNTIME_ERROR = ("Only an admin can make an app a Docker Compose app: compose runs through the Docker "
                      "daemon, as root.")
_ENTRY_EXAMPLE = {"node": "server.js", "bun": "index.ts", "deno": "main.ts", "php": "index.php", "static": "dist",
                  "command": "./server", "java": "target/app.jar"}
_STATIC_DOCROOTS = ("public", "dist", "build", "site", "www", "html")


# ---------------------------------------------------------------------------
# Host tools
# ---------------------------------------------------------------------------

def tool_search_path() -> str:
    """PATH to look for host tools on: Venvy's own, plus where rustup puts cargo."""
    parts = [p for p in os.environ.get("PATH", "").split(":") if p]
    for extra in (str(Path.home() / ".cargo" / "bin"), "/usr/local/bin", "/usr/bin", "/bin"):
        if extra not in parts:
            parts.append(extra)
    return ":".join(parts)


def missing_tools(key: str, packages: bool = False, extra_path: Optional[List[str]] = None) -> List[str]:
    rt = RUNTIMES[key]
    wanted = rt.tools + (rt.pkg_tools if packages else ())
    path = ":".join(list(extra_path or []) + [tool_search_path()])
    return [t for t in wanted if not shutil.which(t, path=path)]


def tools_error(key: str, packages: bool = False, extra_path: Optional[List[str]] = None) -> Optional[str]:
    """Why this runtime cannot be used on this host, or None."""
    missing = missing_tools(key, packages, extra_path)
    if not missing:
        return None
    rt = RUNTIMES[key]
    return (f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not installed on this host, "
            f"so {rt.label} apps cannot run. Install: {rt.install_hint}")


_PY_VERSION_RE = re.compile(r"3\.\d{1,2}")


def python_versions() -> List[str]:
    """Minor versions of Python 3 available for virtualenvs: installed next to
    the default one (python3.X), or from the toolchains folder (uv)."""
    from venvy import toolchains
    path = tool_search_path()
    have = {f"3.{minor}" for minor in range(8, 20) if shutil.which(f"python3.{minor}", path=path)}
    for v in toolchains.installed("python"):
        have.add(".".join(v.split(".")[:2]))
    return sorted(have, key=lambda v: int(v.split(".")[1]))


def python_binary(version: Optional[str]) -> str:
    """The interpreter to build a virtualenv with: python3.X for a chosen version, else python3."""
    if not version:
        return "python3"
    if shutil.which(f"python{version}", path=tool_search_path()):
        return f"python{version}"
    from venvy import toolchains
    return toolchains.python_binary(version) or f"python{version}"


def clean_python_version(value: Optional[str], allow_install: bool = False) -> Optional[str]:
    """A Python version the host has ('' or None: the system's python3). Raises ValueError.
    With *allow_install*, a version uv can install is accepted too."""
    from venvy import toolchains
    v = (value or "").strip()
    if not v:
        return None
    if not _PY_VERSION_RE.fullmatch(v):
        raise ValueError(f"'{v}' is not a Python version like 3.12.")
    if v in python_versions():
        return v
    if allow_install and toolchains.describe()[toolchains.KINDS.index("python")]["can_install"]:
        return v
    have = ", ".join(python_versions()) or "none besides the default"
    raise ValueError(f"Python {v} is not installed on this host (available: {have}). "
                     "Install it under System → Toolchains (needs uv).")


def clean_runtime_version(key: str, value: Optional[str]) -> Optional[str]:
    """The version setting of a non-Python app ('' = the host's own tool)."""
    from venvy import toolchains
    kind = toolchains.RUNTIME_KIND.get(key)
    v = (value or "").strip()
    if not v:
        return None
    if key == "python":
        return clean_python_version(v, allow_install=True)
    if not kind:
        raise ValueError(f"{RUNTIMES[key].label} apps have no version to choose.")
    return toolchains.clean_version(kind, v)


def describe() -> List[dict]:
    """The runtimes as the UI needs them (GET /api/runtimes)."""
    from venvy import toolchains
    return [{
        "key": rt.key, "label": rt.label,
        "installed": not missing_tools(rt.key),
        "packages_installed": not missing_tools(rt.key, packages=True),
        "install_hint": rt.install_hint,
        "entry": rt.entry, "entry_default": rt.entry_default, "entry_label": rt.entry_label,
        "entry_placeholder": rt.entry_placeholder, "entry_help": rt.entry_help,
        "packages": rt.packages, "pkg_help": rt.pkg_help, "pkg_example": rt.pkg_example,
        "can_sync": bool(sync_steps(rt.key, Path("/nonexistent"), assume_manifest=True)),
        "versions": python_versions() if rt.key == "python" else toolchains.installed(toolchains.RUNTIME_KIND.get(rt.key, "")) if toolchains.RUNTIME_KIND.get(rt.key) else [],
        "versioned": rt.key in toolchains.RUNTIME_KIND,
    } for rt in RUNTIMES.values()]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

# Docker image references: [registry/]name[:tag][@sha256:digest]
_IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._\-/:@]{0,250}")
_BIN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-]{0,63}")
# Runtimes whose entrypoint may be a path inside the app (src/app.py, bin/server).
_PATH_ENTRY = ("python", "node", "bun", "deno", "ruby", "java", "command", "php", "static", "go", "compose")


def clean_entrypoint(key: str, raw: str) -> str:
    """The entrypoint to store for *raw* as typed. Raises ValueError with a
    message for the user when it is unusable."""
    from venvy.appspec import clean_rel_path
    rt = RUNTIMES[key]
    if rt.entry == FIXED:
        return rt.entry_default
    ep = (raw or "").strip()
    if ep == "":
        return ""
    if key == "docker":
        if not _IMAGE_RE.fullmatch(ep) or ep.startswith("-") or ".." in ep:
            raise ValueError("The image must be a reference like nginx:1.27 or ghcr.io/org/app:tag.")
        return ep
    if key == "rust":
        if not _BIN_RE.fullmatch(ep):
            raise ValueError("The binary must be a plain name like api (as in cargo build --bin api).")
        return ep
    if key == "go" and ep in (".", "./"):
        return ""
    noun = "folder" if key == "static" else ("package" if key == "go" else "file")
    example = _ENTRY_EXAMPLE.get(key) or rt.entry_default or "main"
    if any(c in ep for c in " \t\"'`$;|&<>"):
        raise ValueError("Entrypoint contains invalid characters.")
    try:
        cleaned = clean_rel_path(ep, f"entrypoint {noun}", allow_empty=False)
    except ValueError:
        raise ValueError(f"Entrypoint must be a {noun} inside the app (e.g. {example}), without '..'.")
    if key not in _PATH_ENTRY and "/" in cleaned:
        raise ValueError(f"Entrypoint must be a plain {noun} name (e.g. {example}), not a path.")
    if not re.fullmatch(r"[A-Za-z0-9_.@+\-/]+", cleaned):
        raise ValueError("Entrypoint filename contains invalid characters.")
    return cleaned


def entry_missing(key: str, entrypoint: str, start_command: Optional[str] = None) -> bool:
    if start_command:
        return False
    if key == "command":
        return not entrypoint
    return RUNTIMES[key].entry == REQUIRED and not entrypoint


def needs_port(key: str, entrypoint: str) -> bool:
    """Runtimes whose command line names the port: Venvy picks one if the
    app has none yet."""
    return key in ("static", "php", "docker", "compose") or (key == "ruby" and entrypoint.endswith(".ru"))


# ---------------------------------------------------------------------------
# Running the app
# ---------------------------------------------------------------------------

def _safe_subdir(app_dir: Path, name: str) -> Optional[Path]:
    """Return app_dir/name only if it is a real directory strictly inside app_dir (never a symlink)."""
    cand = app_dir / name
    if cand.is_symlink() or not cand.is_dir():
        return None
    try:
        real_root = app_dir.resolve()
        real_cand = cand.resolve()
        if real_cand == real_root or real_root in real_cand.parents:
            return cand
    except OSError:
        pass
    return None


def _inside(root: Path, cand: Path) -> bool:
    try:
        real_root, real = root.resolve(), cand.resolve()
    except OSError:
        return False
    return real == real_root or real_root in real.parents


def static_docroot(app_dir: Path, entrypoint: str) -> Path:
    if entrypoint:
        return app_dir / entrypoint
    for name in _STATIC_DOCROOTS:
        sub = _safe_subdir(app_dir, name)
        if sub is not None:
            return sub
    return app_dir


def _toml_file(app_dir: Path, name: str) -> dict:
    from venvy.config import _toml
    f = app_dir / name
    if _toml is None or not f.is_file() or f.is_symlink():
        return {}
    try:
        return _toml.loads(f.read_text(encoding="utf-8", errors="replace")[:500_000])
    except Exception:
        return {}


def rust_binary(app_dir: Path, entrypoint: str) -> Optional[str]:
    """The binary cargo build --release makes: the one named, else the package's
    only one. None when it can't be told (a workspace, several [[bin]])."""
    if entrypoint:
        return entrypoint
    cargo = _toml_file(app_dir, "Cargo.toml")
    bins = cargo.get("bin") if isinstance(cargo.get("bin"), list) else []
    names = [b.get("name") for b in bins if isinstance(b, dict) and b.get("name")]
    if len(names) == 1:
        return str(names[0])
    if len(names) > 1:
        return None
    pkg = cargo.get("package")
    if isinstance(pkg, dict) and pkg.get("name"):
        return str(pkg["name"])
    return None


def _rust_target_dir(app_dir: Path) -> Path:
    cfg = _toml_file(app_dir, ".cargo/config.toml")
    build = cfg.get("build") if isinstance(cfg.get("build"), dict) else {}
    td = build.get("target-dir") if isinstance(build, dict) else None
    if isinstance(td, str) and td and not td.startswith("/") and ".." not in td:
        return app_dir / td
    return app_dir / "target"


def rust_artifact(app_dir: Path, entrypoint: str) -> Optional[Path]:
    name = rust_binary(app_dir, entrypoint)
    return _rust_target_dir(app_dir) / "release" / name if name else None


GO_ARTIFACT = Path(".venvy") / "bin" / "app"


def go_package(entrypoint: str) -> str:
    if not entrypoint:
        return "."
    return entrypoint if entrypoint.startswith("./") else "./" + entrypoint


_JAR_SKIP = ("-sources.jar", "-javadoc.jar", "-plain.jar", "-tests.jar")


def java_artifact(app_dir: Path, entrypoint: str) -> Optional[Path]:
    if entrypoint:
        return app_dir / entrypoint
    for sub in ("target", "build/libs"):
        d = app_dir / sub
        if not d.is_dir() or d.is_symlink():
            continue
        jars = [j for j in d.glob("*.jar") if not j.name.endswith(_JAR_SKIP) and j.is_file()]
        if jars:
            return max(jars, key=lambda j: j.stat().st_mtime)
    jars = sorted(app_dir.glob("*.jar"))
    return jars[0] if jars else None


def compose_file(app_dir: Path, entrypoint: str) -> Optional[Path]:
    if entrypoint:
        return app_dir / entrypoint
    for n in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
        if (app_dir / n).is_file():
            return app_dir / n
    return None


def node_pm(app_dir: Path) -> str:
    """npm, pnpm or yarn — by the lockfile (or packageManager) the app has."""
    if (app_dir / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (app_dir / "yarn.lock").is_file():
        return "yarn"
    pkg = _json(_read_small(app_dir / "package.json"))
    pm = str(pkg.get("packageManager") or "")
    if pm.startswith("pnpm@"):
        return "pnpm"
    if pm.startswith("yarn@"):
        return "yarn"
    return "npm"


def _read_small(path: Path) -> Optional[str]:
    try:
        if path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8", errors="replace")[:1_000_000]
    except OSError:
        pass
    return None


def _pm_argv(pm: str, *args: str) -> List[str]:
    """pnpm/yarn through corepack when they aren't installed themselves."""
    if pm in ("pnpm", "yarn") and not shutil.which(pm, path=tool_search_path()) and shutil.which("corepack", path=tool_search_path()):
        return ["corepack", pm, *args]
    return [pm, *args]


def docker_name(app_id: int) -> str:
    return f"venvy-app-{app_id}"


def docker_image(owner: int) -> str:
    return f"venvy-app-{owner}:latest"


def start_command(key: str, app_dir: Path, entrypoint: str, port: Optional[int],
                  venv_path: str = "", host: str = "0.0.0.0", app=None) -> List[str]:
    """The argv that runs the app, from its (working) directory."""
    if key == "python":
        return [str(Path(venv_path) / "bin" / "python"), entrypoint]
    if key == "rust":
        art = rust_artifact(app_dir, entrypoint)
        if art is None:
            raise ValueError("This crate has several binaries (or is a workspace): name the one to run in Settings → Binary.")
        return [str(art)]
    if key == "node":
        if entrypoint:
            return ["node", entrypoint]
        pm = node_pm(app_dir)
        return _pm_argv(pm, "start") if pm != "npm" else ["npm", "start", "--silent"]
    if key == "bun":
        return ["bun", "run", f"./{entrypoint}"] if entrypoint else ["bun", "run", "start"]
    if key == "deno":
        return ["deno", "run", "--allow-all", entrypoint] if entrypoint else ["deno", "task", "start"]
    if key == "go":
        return [str(app_dir / GO_ARTIFACT)]
    if key == "java":
        jar = java_artifact(app_dir, entrypoint)
        if jar is None:
            raise ValueError("No JAR to run: build the app (Deploy) or name the JAR in Settings.")
        return ["java", "-jar", str(jar)]
    if key == "ruby":
        bundled = (app_dir / "Gemfile").is_file()
        pre = ["bundle", "exec"] if bundled else []
        if entrypoint.endswith(".ru"):
            return pre + ["rackup", "--host", host, "--port", str(port), entrypoint]
        return pre + ["ruby", entrypoint]
    if key == "php":
        docroot = _safe_subdir(app_dir, "public") or app_dir
        router = entrypoint or str(_ASSET_DIR / "router.php")
        return ["php", "-S", f"{host}:{port}", "-t", str(docroot), router]
    if key == "static":
        docroot = static_docroot(app_dir, entrypoint)
        cmd = ["python3", "-I", str(_ASSET_DIR / "static_server.py"),
               "--dir", str(docroot), "--root", str(app_dir), "--host", host, "--port", str(port)]
        if docroot == app_dir:
            cmd += ["--deny", "logs,venv,node_modules,.venvy,.git"]
        return cmd
    if key == "command":
        if not entrypoint:
            raise ValueError("Set a start command (or name the executable) in Settings.")
        return [str(app_dir / entrypoint)]
    if key == "docker":
        if app is None:
            raise ValueError("docker apps need their settings")
        return docker_run_argv(app, app_dir, entrypoint, port, host)
    if key == "compose":
        if app is None:
            raise ValueError("compose apps need their settings")
        f = compose_file(app_dir, entrypoint)
        if f is None:
            raise ValueError("There is no compose.yaml or docker-compose.yml in the app's folder.")
        return ["docker", "compose", "-p", docker_name(app.id), "-f", str(f), "--project-directory", str(app_dir),
                "up", "--remove-orphans", "--no-color"]
    raise ValueError(f"unknown runtime {key!r}")


def docker_run_argv(app, app_dir: Path, entrypoint: str, port: Optional[int], host: str) -> List[str]:
    """docker run for a docker app. Settings that would weaken the host
    (privileged, host network, other mounts) can't be expressed here at all."""
    from venvy import appspec
    image = entrypoint or docker_image(appspec.owner_id(app))
    cport = app.container_port or 8080
    argv = ["docker", "run", "--rm", "--init", "--name", docker_name(app.id),
            "--security-opt", "no-new-privileges", "--pull", "missing",
            "--label", f"venvy.app={app.id}",
            "-v", f"{appspec.data_dir(app)}:/data"]
    if port:
        argv += ["-p", f"{host}:{port}:{cport}"]
    if app.memory_limit_mb:
        argv += ["--memory", f"{app.memory_limit_mb}m"]
    if app.cpu_limit_percent:
        argv += ["--cpus", f"{app.cpu_limit_percent / 100:.2f}"]
    if app.pids_limit:
        argv += ["--pids-limit", str(app.pids_limit)]
    if app.egress_policy == "deny":
        argv += ["--network", "none"] if not port else []
    stop_t = app.stop_timeout
    if stop_t:
        argv += ["--stop-timeout", str(stop_t)]
    if app.stop_signal and app.stop_signal != "SIGTERM":
        argv += ["--stop-signal", app.stop_signal]
    argv += ["--env-file", "{ENV_FILE}", image]
    if app.start_command:
        argv += ["{START_ARGS}"]
    return argv


def start_problem(key: str, app_dir: Path, entrypoint: str) -> Optional[str]:
    """Something that would make the app fail at once, said in words — or None."""
    if key == "static" and entrypoint:
        candidate = app_dir / entrypoint
        if candidate.is_symlink() or not _inside(app_dir, candidate) or not candidate.is_dir():
            return f"The site folder '{entrypoint}' does not exist. Upload it, or clear the setting to pick one automatically."
    if key == "go" and not (app_dir / "go.mod").is_file():
        return "There is no go.mod in the app's folder. Upload one, or run go mod init in the Terminal tab."
    if key == "rust" and not (app_dir / "Cargo.toml").is_file():
        return "There is no Cargo.toml in the app's folder."
    if key == "rust" and rust_binary(app_dir, entrypoint) is None:
        return "This crate has several binaries (or is a workspace): name the one to run in Settings → Binary."
    if key == "command" and entrypoint:
        cand = app_dir / entrypoint
        if not _inside(app_dir, cand) or not cand.is_file():
            return f"The executable '{entrypoint}' does not exist in the app's folder."
    if key == "docker" and not entrypoint and not (app_dir / "Dockerfile").is_file() and not (app_dir / "Containerfile").is_file():
        return "Name an image in Settings, or upload a Dockerfile."
    if key == "compose" and compose_file(app_dir, entrypoint) is None:
        return "There is no compose file in the app's folder."
    return None


def run_env(key: str, app_dir: Path, port: Optional[int]) -> Tuple[Dict[str, str], List[str]]:
    """(variables to set unless the app sets them itself, directories to put
    first on PATH) for a running app."""
    env: Dict[str, str] = {}
    path: List[str] = []
    if key in ("node", "bun"):
        env["NODE_ENV"] = "production"
        path.append(str(app_dir / "node_modules" / ".bin"))
    elif key == "deno":
        env["DENO_NO_UPDATE_CHECK"] = "1"
        path.append(str(app_dir / "node_modules" / ".bin"))
    elif key == "ruby":
        env["BUNDLE_PATH"] = str(app_dir / "vendor" / "bundle")
        env["RACK_ENV"] = "production"
    elif key == "php":
        env["PHP_CLI_SERVER_WORKERS"] = "4"
        path.append(str(app_dir / "vendor" / "bin"))
    elif key == "go":
        if not (app_dir / "vendor").is_dir():
            env["GOFLAGS"] = "-mod=mod"      # fill in go.sum instead of refusing to start
    elif key == "java" and port:
        env["SERVER_PORT"] = str(port)
    elif key == "rust":
        env.setdefault("RUST_BACKTRACE", "1")
    return env, path


def tool_env(key: str, app_dir: Path) -> Dict[str, str]:
    """Extra environment for package-manager and set-up commands."""
    if key == "ruby":
        return {"BUNDLE_PATH": str(app_dir / "vendor" / "bundle")}
    if key == "php":
        return {"COMPOSER_ALLOW_SUPERUSER": "1", "COMPOSER_NO_INTERACTION": "1"}
    if key == "deno":
        return {"DENO_NO_UPDATE_CHECK": "1"}
    if key == "go":
        return {"GOFLAGS": "-mod=mod"}
    if key in ("node", "bun", "static"):
        return {"COREPACK_ENABLE_DOWNLOAD_PROMPT": "0", "CI": "1"}
    return {}


# ---------------------------------------------------------------------------
# Set-up from the app's manifest
# ---------------------------------------------------------------------------

def _pyproject(app_dir: Path) -> dict:
    return _toml_file(app_dir, "pyproject.toml")


def python_steps(app_dir: Path, venv_path: str, assume_manifest: bool = False) -> List[Step]:
    """Install a Python app's dependencies into its virtualenv, by what it has:
    uv.lock (uv), poetry.lock (Poetry), Pipfile.lock (Pipenv), requirements.txt,
    or the dependencies a pyproject.toml lists."""
    def has(name: str) -> bool:
        return assume_manifest or (app_dir / name).is_file()

    venv = Path(venv_path)
    # python -m pip, not bin/pip: the script's shebang names the folder the
    # virtualenv was made in, which an atomic deploy's build folder is not.
    pip_argv = [str(venv / "bin" / "python"), "-m", "pip"]
    path = tool_search_path()
    venv_env = (("VIRTUAL_ENV", str(venv)),)
    if has("uv.lock") and shutil.which("uv", path=path):
        return [Step(["uv", "sync", "--frozen", "--no-dev"], 900, "uv sync", True,
                     env=(("UV_PROJECT_ENVIRONMENT", str(venv)), ("UV_PYTHON_DOWNLOADS", "never")))]
    if has("poetry.lock") and shutil.which("poetry", path=path):
        return [Step(["poetry", "install", "--no-root", "--only", "main", "--no-interaction"], 900, "poetry install", True,
                     env=venv_env + (("POETRY_VIRTUALENVS_CREATE", "false"),))]
    if has("Pipfile.lock") and shutil.which("pipenv", path=path):
        return [Step(["pipenv", "sync"], 900, "pipenv sync", True,
                     env=venv_env + (("PIPENV_IGNORE_VIRTUALENVS", "0"), ("PIPENV_VERBOSITY", "-1")))]
    if has("requirements.txt"):
        return [Step(pip_argv + ["install", "--no-input", "-r", str(app_dir / "requirements.txt")], 900,
                     "pip install -r requirements.txt")]
    if assume_manifest:
        return []
    py = _pyproject(app_dir)
    project = py.get("project") if isinstance(py.get("project"), dict) else {}
    if project and isinstance(py.get("build-system"), dict):
        return [Step(pip_argv + ["install", "--no-input", "."], 900, "pip install .", True)]
    deps = project.get("dependencies") if isinstance(project, dict) else None
    if isinstance(deps, list) and deps and all(isinstance(d, str) and not d.startswith("-") for d in deps):
        return [Step(pip_argv + ["install", "--no-input", "--", *deps], 900, "pip install (pyproject.toml)")]
    return []


def sync_steps(key: str, app_dir: Path, assume_manifest: bool = False, venv_path: str = "") -> List[Step]:
    """Install everything the app's manifest lists. Empty when the runtime has
    no manifest, or the app has none yet."""
    def has(name: str) -> bool:
        return assume_manifest or (app_dir / name).is_file()

    if key == "python":
        return python_steps(app_dir, venv_path or str(app_dir / "venv"), assume_manifest) if venv_path or assume_manifest else []
    if key in ("node", "static") and has("package.json"):
        if key == "static" and not shutil.which("npm", path=tool_search_path()) and not assume_manifest:
            return []
        pm = "npm" if assume_manifest else node_pm(app_dir)
        if pm == "pnpm":
            return [Step(_pm_argv("pnpm", "install", "--frozen-lockfile"), 900, "pnpm install", True)]
        if pm == "yarn":
            berry = (app_dir / ".yarnrc.yml").is_file()
            return [Step(_pm_argv("yarn", "install", "--immutable" if berry else "--frozen-lockfile"), 900, "yarn install", True)]
        lock = has("package-lock.json") or has("npm-shrinkwrap.json")
        if lock and not assume_manifest:
            return [Step(["npm", "ci", "--prefix", str(app_dir), "--no-audit", "--no-fund"], 900, "npm ci")]
        return [Step(["npm", "install", "--prefix", str(app_dir), "--no-audit", "--no-fund"], 900, "npm install")]
    if key == "bun" and has("package.json"):
        frozen = ["--frozen-lockfile"] if (app_dir / "bun.lockb").is_file() or (app_dir / "bun.lock").is_file() else []
        return [Step(["bun", "install", *frozen], 600, "bun install", True)]
    if key == "deno" and (has("deno.json") or has("deno.jsonc") or has("package.json")):
        return [Step(["deno", "install"], 600, "deno install", True)]
    if key == "go" and has("go.mod"):
        return [Step(["go", "mod", "download"], 600, "go mod download", True)]
    if key == "rust" and has("Cargo.toml"):
        locked = ["--locked"] if (app_dir / "Cargo.lock").is_file() else []
        return [Step(["cargo", "fetch", *locked], 900, "cargo fetch", True)]
    if key == "php" and has("composer.json"):
        return [Step(["composer", "install", "--no-dev", "--no-interaction", "--no-progress", "--prefer-dist"],
                     600, "composer install", True)]
    if key == "ruby" and has("Gemfile"):
        return [Step(["bundle", "install"], 600, "bundle install", True)]
    return []


def build_steps(key: str, app_dir: Path, entrypoint: str = "", app=None) -> List[Step]:
    """Compile the app — what makes the thing start_command runs. Empty for
    runtimes that run their sources as they are."""
    if key == "rust" and (app_dir / "Cargo.toml").is_file():
        argv = ["cargo", "build", "--release"]
        if (app_dir / "Cargo.lock").is_file():
            argv.append("--locked")
        if entrypoint:
            argv += ["--bin", entrypoint]
        return [Step(argv, 3600, "cargo build --release", True)]
    if key == "go" and (app_dir / "go.mod").is_file():
        return [Step(["go", "build", "-trimpath", "-o", str(app_dir / GO_ARTIFACT), go_package(entrypoint)],
                     1800, "go build", True)]
    if key == "java":
        if (app_dir / "pom.xml").is_file():
            tool = "./mvnw" if (app_dir / "mvnw").is_file() else "mvn"
            argv = ["sh", tool] if tool == "./mvnw" else [tool]
            return [Step(argv + ["-B", "-q", "-DskipTests", "package"], 1800, "maven package", True)]
        if (app_dir / "build.gradle").is_file() or (app_dir / "build.gradle.kts").is_file():
            tool = "./gradlew" if (app_dir / "gradlew").is_file() else "gradle"
            argv = ["sh", tool] if tool == "./gradlew" else [tool]
            return [Step(argv + ["--no-daemon", "-q", "build", "-x", "test"], 1800, "gradle build", True)]
        return []
    if key in ("node", "bun", "static") and (app_dir / "package.json").is_file():
        pkg = _json(_read_small(app_dir / "package.json"))
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        if "build" not in scripts:
            return []
        if key == "bun":
            return [Step(["bun", "run", "build"], 1800, "bun run build", True)]
        if key == "static" and not shutil.which("npm", path=tool_search_path()):
            return []
        pm = node_pm(app_dir)
        return [Step(_pm_argv(pm, "run", "build"), 1800, f"{pm} run build", True)]
    if key == "docker" and not entrypoint and app is not None:
        from venvy import appspec
        df = "Containerfile" if (app_dir / "Containerfile").is_file() and not (app_dir / "Dockerfile").is_file() else "Dockerfile"
        if not (app_dir / df).is_file():
            return []
        return [Step(["docker", "build", "--pull", "-t", docker_image(appspec.owner_id(app)), "-f", str(app_dir / df),
                      str(app_dir)], 3600, "docker build", True, as_venvy=True)]
    if key == "compose" and app is not None:
        f = compose_file(app_dir, entrypoint)
        if f is None:
            return []
        return [Step(["docker", "compose", "-p", docker_name(app.id), "-f", str(f), "--project-directory", str(app_dir),
                      "build", "--pull"], 3600, "docker compose build", True, as_venvy=True),
                Step(["docker", "compose", "-p", docker_name(app.id), "-f", str(f), "--project-directory", str(app_dir),
                      "pull", "--ignore-buildable", "--quiet"], 1800, "docker compose pull", True, as_venvy=True)]
    return []


def artifact_missing(key: str, app_dir: Path, entrypoint: str) -> bool:
    """True when the app has to be built before it can start at all."""
    if key == "rust":
        art = rust_artifact(app_dir, entrypoint)
        return art is not None and not art.is_file()
    if key == "go":
        return not (app_dir / GO_ARTIFACT).is_file()
    if key == "java" and ((app_dir / "pom.xml").is_file() or (app_dir / "build.gradle").is_file()
                          or (app_dir / "build.gradle.kts").is_file()):
        return java_artifact(app_dir, entrypoint) is None
    return False


def first_run_steps(key: str, app_dir: Path, entrypoint: str = "", app=None) -> List[Step]:
    """What a start has to do first because the dependencies were never
    installed (or the app never built) — files uploaded after the app was
    created, or restored without them. Only where "the folder is missing" is
    a reliable sign."""
    steps: List[Step] = []
    missing = {
        "node": "node_modules", "bun": "node_modules",
        "php": "vendor", "ruby": "vendor/bundle",
    }.get(key)
    if missing and not (app_dir / missing).exists():
        steps += sync_steps(key, app_dir)
    if artifact_missing(key, app_dir, entrypoint):
        steps += build_steps(key, app_dir, entrypoint, app)
    if key == "docker" and app is not None and not entrypoint and build_steps(key, app_dir, entrypoint, app):
        import subprocess
        try:
            r = subprocess.run(["docker", "image", "inspect", docker_image(int(app.parent_app_id or app.id))],
                               capture_output=True, timeout=20)
            if r.returncode != 0:
                steps += build_steps(key, app_dir, entrypoint, app)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return steps


def setup_steps(key: str, app_dir: Path, entrypoint: str = "", app=None) -> List[Step]:
    """What to run once, while a new app is being created. Deno resolves its
    imports on first run, so (unlike `deno install`, which needs Deno 2) it
    has nothing to do here."""
    if key == "deno":
        return []
    return sync_steps(key, app_dir) + build_steps(key, app_dir, entrypoint, app)


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

_NPM_NAME = r"(@[a-z0-9][a-z0-9_.\-]*/)?[A-Za-z0-9][A-Za-z0-9_.\-]*"
_VERSION = r"[A-Za-z0-9_.+~^<>=*][A-Za-z0-9_.+~^<>=*\-]*"
_RE = {
    "npm": re.compile(rf"{_NPM_NAME}(@{_VERSION})?"),
    "npm_name": re.compile(_NPM_NAME),
    "go": re.compile(r"[A-Za-z0-9][A-Za-z0-9._~\-]*(/[A-Za-z0-9._~\-]+)*(@[A-Za-z0-9_.+\-]+)?"),
    "go_name": re.compile(r"[A-Za-z0-9][A-Za-z0-9._~\-]*(/[A-Za-z0-9._~\-]+)*"),
    "deno": re.compile(rf"(jsr|npm):(@[a-z0-9][a-z0-9_.\-]*/)?[A-Za-z0-9][A-Za-z0-9_.\-]*(@{_VERSION})?"),
    "deno_name": re.compile(r"(@[A-Za-z0-9][A-Za-z0-9_.\-]*/)?[A-Za-z0-9][A-Za-z0-9_.\-]*"),
    "composer": re.compile(rf"[a-z0-9][a-z0-9_.\-]*/[a-z0-9][a-z0-9_.\-]*([:@]{_VERSION})?"),
    "composer_name": re.compile(r"[a-z0-9][a-z0-9_.\-]*/[a-z0-9][a-z0-9_.\-]*"),
    "gem": re.compile(rf"[A-Za-z0-9][A-Za-z0-9_.\-]*(@{_VERSION})?"),
    "gem_name": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*"),
}
_FAMILY = {"node": "npm", "bun": "npm", "go": "go", "deno": "deno", "php": "composer", "ruby": "gem"}


def can_install(key: str) -> bool:
    return key in _FAMILY


def _check(key: str, name: str, whole: bool) -> str:
    pattern = _RE[_FAMILY[key] + ("" if whole else "_name")]
    if not pattern.fullmatch(name) or name.startswith("-"):
        raise ValueError("Invalid package name.")
    return name


def install_steps(key: str, app_dir: Path, spec: str) -> List[Step]:
    """Add one package. *spec* is what the user typed: a name, optionally with
    a version after @ (or : for Composer)."""
    spec = _check(key, spec.strip(), whole=True)
    if key == "node":
        return [Step(["npm", "install", "--prefix", str(app_dir), "--no-audit", "--no-fund", spec], 600, "npm install")]
    if key == "bun":
        return [Step(["bun", "add", spec], 600, "bun add", True)]
    if key == "deno":
        return [Step(["deno", "add", spec], 300, "deno add", True)]
    if key == "go":
        return [Step(["go", "get", spec], 600, "go get", True)]
    if key == "php":
        return [Step(["composer", "require", spec.replace("@", ":", 1), "--no-interaction", "--no-progress"],
                     600, "composer require", True)]
    if key == "ruby":
        name, _, version = spec.partition("@")
        argv = ["bundle", "add", name] + (["--version", version] if version else [])
        return [Step(argv, 600, "bundle add", True)]
    raise ValueError("Packages are not managed for this runtime.")


def remove_steps(key: str, app_dir: Path, name: str) -> List[Step]:
    name = _check(key, name.strip(), whole=False)
    if key == "node":
        return [Step(["npm", "uninstall", "--prefix", str(app_dir), "--no-audit", "--no-fund", name], 300, "npm uninstall")]
    if key == "bun":
        return [Step(["bun", "remove", name], 300, "bun remove", True)]
    if key == "deno":
        return [Step(["deno", "remove", name], 300, "deno remove", True)]
    if key == "go":
        return [Step(["go", "mod", "edit", f"-droprequire={name}"], 60, "go mod edit", True),
                Step(["go", "mod", "tidy"], 300, "go mod tidy", True)]
    if key == "php":
        return [Step(["composer", "remove", name, "--no-interaction", "--no-progress"], 300, "composer remove", True)]
    if key == "ruby":
        return [Step(["bundle", "remove", name], 300, "bundle remove", True)]
    raise ValueError("Packages are not managed for this runtime.")


# --- Reading the manifests --------------------------------------------------
# `read(name)` returns the text of a file in the app's directory (or None);
# the caller decides how to open it safely.

def _json(text: Optional[str]) -> dict:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = json.loads(_strip_jsonc(text))
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def _strip_jsonc(text: str) -> str:
    """Comments and trailing commas out of JSON-with-comments (deno.jsonc)."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True; out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    # Strip trailing commas outside string literals so strings like ",}" stay intact.
    stripped = "".join(out)
    res, j, m, in_str = [], 0, len(stripped), False
    while j < m:
        c = stripped[j]
        if in_str:
            res.append(c)
            if c == "\\" and j + 1 < m:
                res.append(stripped[j + 1]); j += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True; res.append(c)
        elif c == ",":
            k = j + 1
            while k < m and stripped[k] in " \t\r\n":
                k += 1
            if k < m and stripped[k] in "}]":
                j += 1
                continue
            res.append(c)
        else:
            res.append(c)
        j += 1
    return "".join(res)


def _deps(data: dict, *sections: str) -> List[dict]:
    rows = []
    for section in sections:
        block = data.get(section)
        if isinstance(block, dict):
            rows += [{"name": str(k), "version": str(v)} for k, v in block.items()]
    return rows


def _go_mod(text: str) -> List[dict]:
    rows, in_block = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if in_block:
            if line.startswith(")"):
                in_block = False
                continue
            spec = line
        elif re.match(r"require\s*\($", line):
            in_block = True
            continue
        elif line.startswith("require "):
            spec = line[len("require "):].strip()
        else:
            continue
        code, _, comment = spec.partition("//")
        fields = code.split()
        if len(fields) >= 2 and "indirect" not in comment:
            rows.append({"name": fields[0], "version": fields[1]})
    return rows


def _gemfile(text: str, lock: Optional[str]) -> List[dict]:
    locked = dict(re.findall(r"^    ([A-Za-z0-9_.\-]+) \(([^)\s,]+)\)", lock or "", re.M))
    rows = []
    for m in re.finditer(r"""^\s*gem\s+['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?""", text, re.M):
        name, constraint = m.group(1), m.group(2)
        rows.append({"name": name, "version": locked.get(name) or constraint or ""})
    return rows


def _pom(text: str) -> List[dict]:
    rows = []
    body = re.sub(r"<dependencyManagement>.*?</dependencyManagement>", "", text or "", flags=re.S)
    for m in re.finditer(r"<dependency>(.*?)</dependency>", body, re.S):
        blk = m.group(1)
        g = re.search(r"<groupId>\s*([^<\s]+)", blk)
        a = re.search(r"<artifactId>\s*([^<\s]+)", blk)
        v = re.search(r"<version>\s*([^<\s]+)", blk)
        if g and a:
            rows.append({"name": f"{g.group(1)}:{a.group(1)}", "version": v.group(1) if v else ""})
    return rows


def _gradle(text: str) -> List[dict]:
    rows = []
    for m in re.finditer(r"""(?:implementation|api|runtimeOnly|compileOnly)\s*\(?\s*['"]([^'":]+):([^'":]+)(?::([^'"]+))?['"]""", text or ""):
        rows.append({"name": f"{m.group(1)}:{m.group(2)}", "version": m.group(3) or ""})
    return rows


def _cargo(text: str) -> List[dict]:
    from venvy.config import _toml
    if _toml is None:
        return []
    try:
        data = _toml.loads(text or "")
    except Exception:
        return []
    rows = []
    for section in ("dependencies", "build-dependencies"):
        block = data.get(section)
        if isinstance(block, dict):
            for k, v in block.items():
                ver = v if isinstance(v, str) else (v.get("version", "") if isinstance(v, dict) else "")
                rows.append({"name": str(k), "version": str(ver)})
    return rows


def list_packages(key: str, read: Callable[[str], Optional[str]]) -> List[dict]:
    if key == "java":
        return _pom(read("pom.xml") or "") or _gradle(read("build.gradle") or read("build.gradle.kts") or "")
    if key == "rust":
        return _cargo(read("Cargo.toml") or "")
    if key in ("node", "bun"):
        return _deps(_json(read("package.json")), "dependencies", "devDependencies")
    if key == "deno":
        cfg = _json(read("deno.json")) or _json(read("deno.jsonc"))
        imports = cfg.get("imports")
        rows = [{"name": str(k), "version": str(v)} for k, v in imports.items()] if isinstance(imports, dict) else []
        return rows + _deps(_json(read("package.json")), "dependencies", "devDependencies")
    if key == "go":
        return _go_mod(read("go.mod") or "")
    if key == "php":
        rows = _deps(_json(read("composer.json")), "require", "require-dev")
        return [r for r in rows if r["name"] != "php" and not re.match(r"(ext|lib)-", r["name"])]
    if key == "ruby":
        return _gemfile(read("Gemfile") or "", read("Gemfile.lock"))
    return []
