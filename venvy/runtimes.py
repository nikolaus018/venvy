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

# A plain file or folder name — an entrypoint is never a path.
_ENTRY_RE = re.compile(r"[A-Za-z0-9_.\-]+")

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
    in_app_dir: bool = False          # run from the app's directory instead of "/"


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
        install_hint="sudo apt-get install -y cargo rustc build-essential   (or re-run the installer with --with-rust)",
        entry=FIXED, entry_default="main.rs", entry_label="Entrypoint File",
        entry_placeholder="main.rs",
        entry_help="Rust apps always start with cargo run.",
        packages=PKG_LIST,
    ),
    Runtime(
        key="go", label="Go",
        tools=("go",),
        install_hint="re-run the installer with --with-go   (https://go.dev/dl)",
        entry=FIXED, entry_default="main.go", entry_label="Entrypoint File",
        entry_placeholder="main.go",
        entry_help="Go apps always start with go run . — the main package in the app's folder.",
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
        key="java", label="Java (JAR)",
        tools=("java",),
        install_hint="sudo apt-get install -y default-jre-headless   (or re-run the installer with --with-java)",
        entry=REQUIRED, entry_default="app.jar", entry_label="JAR File",
        entry_placeholder="app.jar",
        entry_help="The executable JAR that launches your app, run with java -jar. The port is passed as PORT and SERVER_PORT (Spring Boot picks that up).",
        pkg_help="Java apps are a single JAR with its dependencies bundled — there is nothing to install here.",
    ),
    Runtime(
        key="static", label="Static site",
        tools=("python3",),
        install_hint="sudo apt-get install -y python3",
        entry=OPTIONAL, entry_default="", entry_label="Site Folder",
        entry_placeholder="blank = public, dist, build, site or the app folder",
        entry_help="The folder that holds the site. Blank picks the first of public, dist, build, site, www or html that exists, else the app's own folder. index.html is the home page; /about serves about.html; a 404.html is used for unknown paths.",
        pkg_help="A static site has no packages.",
    ),
)}

RUNTIME_KEYS: Tuple[str, ...] = tuple(RUNTIMES)
RUNTIME_ERROR = "runtime must be one of: " + ", ".join(RUNTIME_KEYS)
_ENTRY_EXAMPLE = {"node": "server.js", "bun": "index.ts", "deno": "main.ts", "php": "index.php", "static": "dist"}
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


def missing_tools(key: str, packages: bool = False) -> List[str]:
    rt = RUNTIMES[key]
    wanted = rt.tools + (rt.pkg_tools if packages else ())
    path = tool_search_path()
    return [t for t in wanted if not shutil.which(t, path=path)]


def tools_error(key: str, packages: bool = False) -> Optional[str]:
    """Why this runtime cannot be used on this host, or None."""
    missing = missing_tools(key, packages)
    if not missing:
        return None
    rt = RUNTIMES[key]
    return (f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not installed on this host, "
            f"so {rt.label} apps cannot run. Install: {rt.install_hint}")


_PY_VERSION_RE = re.compile(r"3\.\d{1,2}")


def python_versions() -> List[str]:
    """Minor versions of Python 3 installed next to the default one (['3.10', '3.12'])."""
    path = tool_search_path()
    return [f"3.{minor}" for minor in range(8, 16) if shutil.which(f"python3.{minor}", path=path)]


def python_binary(version: Optional[str]) -> str:
    """The interpreter to build a virtualenv with: python3.X for a chosen version, else python3."""
    return f"python{version}" if version else "python3"


def clean_python_version(value: Optional[str]) -> Optional[str]:
    """A Python version the host has ('' or None: the system's python3). Raises ValueError."""
    v = (value or "").strip()
    if not v:
        return None
    if not _PY_VERSION_RE.fullmatch(v) or v not in python_versions():
        have = ", ".join(python_versions()) or "none besides the default"
        raise ValueError(f"Python {v} is not installed on this host (available: {have}).")
    return v


def describe() -> List[dict]:
    """The runtimes as the UI needs them (GET /api/runtimes)."""
    return [{
        "key": rt.key, "label": rt.label,
        "installed": not missing_tools(rt.key),
        "packages_installed": not missing_tools(rt.key, packages=True),
        "install_hint": rt.install_hint,
        "entry": rt.entry, "entry_default": rt.entry_default, "entry_label": rt.entry_label,
        "entry_placeholder": rt.entry_placeholder, "entry_help": rt.entry_help,
        "packages": rt.packages, "pkg_help": rt.pkg_help, "pkg_example": rt.pkg_example,
        "can_sync": bool(sync_steps(rt.key, Path("/nonexistent"), assume_manifest=True)),
        "versions": python_versions() if rt.key == "python" else [],
    } for rt in RUNTIMES.values()]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def clean_entrypoint(key: str, raw: str) -> str:
    """The entrypoint to store for *raw* as typed. Raises ValueError with a
    message for the user when it is unusable."""
    rt = RUNTIMES[key]
    if rt.entry == FIXED:
        return rt.entry_default
    ep = (raw or "").strip()
    if ep == "":
        return ""
    name = Path(ep).name
    if not name or name != ep:
        noun = "folder name" if key == "static" else "filename"
        raise ValueError(f"Entrypoint must be a plain {noun} (e.g. {rt.entry_default or _ENTRY_EXAMPLE.get(key, "main")}), not a path.")
    if not _ENTRY_RE.fullmatch(name):
        raise ValueError("Entrypoint filename contains invalid characters.")
    return name


def entry_missing(key: str, entrypoint: str) -> bool:
    return RUNTIMES[key].entry == REQUIRED and not entrypoint


def needs_port(key: str, entrypoint: str) -> bool:
    """Runtimes whose command line names the port: Venvy picks one if the
    app has none yet."""
    return key in ("static", "php") or (key == "ruby" and entrypoint.endswith(".ru"))


# ---------------------------------------------------------------------------
# Running the app
# ---------------------------------------------------------------------------

def static_docroot(app_dir: Path, entrypoint: str) -> Path:
    if entrypoint:
        return app_dir / entrypoint
    for name in _STATIC_DOCROOTS:
        if (app_dir / name).is_dir():
            return app_dir / name
    return app_dir


def start_command(key: str, app_dir: Path, entrypoint: str, port: Optional[int],
                  venv_path: str = "") -> List[str]:
    """The argv that runs the app, from its directory."""
    if key == "python":
        return [str(Path(venv_path) / "bin" / "python"), entrypoint]
    if key == "rust":
        return ["cargo", "run"]
    if key == "node":
        return ["node", entrypoint] if entrypoint else ["npm", "start", "--silent"]
    if key == "bun":
        return ["bun", "run", f"./{entrypoint}"] if entrypoint else ["bun", "run", "start"]
    if key == "deno":
        return ["deno", "run", "--allow-all", entrypoint] if entrypoint else ["deno", "task", "start"]
    if key == "go":
        return ["go", "run", "."]
    if key == "java":
        return ["java", "-jar", entrypoint]
    if key == "ruby":
        bundled = (app_dir / "Gemfile").is_file()
        pre = ["bundle", "exec"] if bundled else []
        if entrypoint.endswith(".ru"):
            return pre + ["rackup", "--host", "0.0.0.0", "--port", str(port), entrypoint]
        return pre + ["ruby", entrypoint]
    if key == "php":
        docroot = app_dir / "public" if (app_dir / "public").is_dir() else app_dir
        router = entrypoint or str(_ASSET_DIR / "router.php")
        return ["php", "-S", f"0.0.0.0:{port}", "-t", str(docroot), router]
    if key == "static":
        docroot = static_docroot(app_dir, entrypoint)
        cmd = ["python3", "-I", str(_ASSET_DIR / "static_server.py"),
               "--dir", str(docroot), "--host", "0.0.0.0", "--port", str(port)]
        if docroot == app_dir:
            cmd += ["--deny", "logs,venv,node_modules"]
        return cmd
    raise ValueError(f"unknown runtime {key!r}")


def start_problem(key: str, app_dir: Path, entrypoint: str) -> Optional[str]:
    """Something that would make the app fail at once, said in words — or None."""
    if key == "static" and entrypoint and not (app_dir / entrypoint).is_dir():
        return f"The site folder '{entrypoint}' does not exist. Upload it, or clear the setting to pick one automatically."
    if key == "go" and not (app_dir / "go.mod").is_file():
        return "There is no go.mod in the app's folder. Upload one, or run go mod init in the Terminal tab."
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
    return {}


# ---------------------------------------------------------------------------
# Set-up from the app's manifest
# ---------------------------------------------------------------------------

def sync_steps(key: str, app_dir: Path, assume_manifest: bool = False) -> List[Step]:
    """Install everything the app's manifest lists. Empty when the runtime has
    no manifest, or the app has none yet."""
    def has(name: str) -> bool:
        return assume_manifest or (app_dir / name).is_file()

    if key == "node" and has("package.json"):
        return [Step(["npm", "install", "--prefix", str(app_dir), "--no-audit", "--no-fund"], 600, "npm install")]
    if key == "bun" and has("package.json"):
        return [Step(["bun", "install"], 600, "bun install", True)]
    if key == "deno" and (has("deno.json") or has("deno.jsonc") or has("package.json")):
        return [Step(["deno", "install"], 600, "deno install", True)]
    if key == "go" and has("go.mod"):
        return [Step(["go", "mod", "download"], 600, "go mod download", True)]
    if key == "php" and has("composer.json"):
        return [Step(["composer", "install", "--no-dev", "--no-interaction", "--no-progress", "--prefer-dist"],
                     600, "composer install", True)]
    if key == "ruby" and has("Gemfile"):
        return [Step(["bundle", "install"], 600, "bundle install", True)]
    return []


def first_run_steps(key: str, app_dir: Path) -> List[Step]:
    """What a start has to do first because the dependencies were never
    installed — files uploaded after the app was created, or restored without
    them. Only where "the folder is missing" is a reliable sign."""
    missing = {
        "node": "node_modules", "bun": "node_modules",
        "php": "vendor", "ruby": "vendor/bundle",
    }.get(key)
    if missing and not (app_dir / missing).exists():
        return sync_steps(key, app_dir)
    return []


def setup_steps(key: str, app_dir: Path) -> List[Step]:
    """What to run once, while a new app is being created. Deno resolves its
    imports on first run, so (unlike `deno install`, which needs Deno 2) it
    has nothing to do here."""
    return [] if key == "deno" else sync_steps(key, app_dir)


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

_NPM_NAME = r"(@[a-z0-9][a-z0-9_.\-]*/)?[A-Za-z0-9][A-Za-z0-9_.\-]*"
_VERSION = r"[A-Za-z0-9_.+~^<>=*\-]+"
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
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


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


def list_packages(key: str, read: Callable[[str], Optional[str]]) -> List[dict]:
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
