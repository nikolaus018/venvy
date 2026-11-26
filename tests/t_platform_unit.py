"""The parts of hosting-anything that need no server: app specs, commands,
runtimes' build and install steps, toolchains (against a local fake
nodejs.org), the systemd unit properties, firewall rules, Caddy config,
services, routing, commit statuses, log shipping and archives."""
import asyncio, hashlib, io, json, os, shutil, sqlite3, subprocess, sys, tarfile, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

repo = sys.argv[1]
WORK = Path(tempfile.mkdtemp(prefix="venvy-unit-"))
os.chdir(WORK)                                   # Venvy keeps its data in the working directory
os.environ["VENVY_TOOLCHAINS_DIR"] = str(WORK / "toolchains")
os.environ["VENVY_TOOLCHAINS_ANY_OS"] = "1"
sys.path.insert(0, repo)
fails = []


def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c:
        fails.append(n)


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return False
    except ValueError:
        return True


from venvy import appspec, runtimes as rt  # noqa: E402

# ── app spec ────────────────────────────────────────────────────────────────
check("rel path normalised", appspec.clean_rel_path("./a/b/") == "a/b" and appspec.clean_rel_path("") == "")
for bad in ("../x", "a/../b", "/etc", "a//b", "-x", "a/-b"):
    check(f"rel path {bad!r} refused", raises(appspec.clean_rel_path, bad))
env = {"PORT": "8080", "X": "1"}
check("$VARS expanded, no shell", appspec.command_argv("./srv --port $PORT ${X} ${Y:-dflt} $$HOME '$PORT'", env)
      == ["./srv", "--port", "8080", "1", "dflt", "$HOME", "8080"])
check("command: one line only", raises(appspec.clean_command, "a\nb") and raises(appspec.clean_command, "a 'b"))
check("command: blank is None", appspec.clean_command("  ") is None)
d = WORK / "wd"
(d / "bin").mkdir(parents=True)
check("relative executable resolved in the app", appspec.command_argv("bin/app -x", {}, d)[0] == str((d / "bin/app").resolve()))
procs = appspec.parse_procfile("web: gunicorn app:app -b 0.0.0.0:$PORT\nworker: celery -A x worker\n# no\nbad line\n-x: y\n")
check("Procfile parsed", procs == {"web": "gunicorn app:app -b 0.0.0.0:$PORT", "worker": "celery -A x worker"}, str(procs))
spec = appspec.parse_spec('runtime = "rust"\nstart = "./target/release/api"\ninstances = 2\nkeep = ["uploads"]\n'
                          '[health]\npath = "/healthz"\ntype = "http"\n[processes]\nworker = "./target/release/api work"\n'
                          '[env]\nRUST_LOG = "info"\n')
check("venvy.toml parsed", spec["fields"] == {"runtime": "rust", "start_command": "./target/release/api", "instances": 2,
                                             "healthcheck_path": "/healthz", "health_type": "http"}
      and spec["processes"] == {"worker": "./target/release/api work"} and spec["env"] == {"RUST_LOG": "info"}
      and spec["keep"] == ["uploads"], str(spec))
for bad in ('nope = 1', '[processes]\nweb = "x"', '[health]\nfoo = 1', 'keep = ["../x"]', 'runtime = '):
    try:
        appspec.parse_spec(bad)
        ok = False
    except appspec.SpecError:
        ok = True
    check(f"venvy.toml refused: {bad!r}", ok)


def detect(files):
    t = Path(tempfile.mkdtemp(dir=WORK))
    for n, c in files.items():
        (t / n).parent.mkdir(parents=True, exist_ok=True)
        (t / n).write_text(c)
    return appspec.detect_runtime(t)[:2]


check("detect rust", detect({"Cargo.toml": "[package]\nname='a'"}) == ("rust", ""))
check("detect go cmd/", detect({"go.mod": "module x", "cmd/api/main.go": ""}) == ("go", "cmd/api"))
check("detect node", detect({"package.json": '{"scripts":{"start":"node s"}}'}) == ("node", ""))
check("detect static build", detect({"package.json": '{"scripts":{"build":"vite build"}}'}) == ("static", ""))
check("detect python", detect({"requirements.txt": "", "app.py": ""}) == ("python", "app.py"))
check("detect compose", detect({"compose.yaml": "services: {}"})[0] == "compose")
check("detect docker", detect({"Dockerfile": "FROM x"}) == ("docker", ""))
check("detect venvy.toml wins", detect({"venvy.toml": 'runtime = "command"\nentrypoint = "bin/x"', "Dockerfile": ""}) == ("command", "bin/x"))
check("detect jar", detect({"app.jar": "x"}) == ("java", "app.jar"))
check("detect nothing", detect({"README": "x"}) == (None, ""))

# ── runtimes: build and install steps ───────────────────────────────────────
r = WORK / "rs"
r.mkdir()
(r / "Cargo.toml").write_text('[package]\nname = "api"\n')
check("rust binary from Cargo.toml", rt.rust_binary(r, "") == "api" and str(rt.rust_artifact(r, "")).endswith("target/release/api"))
(r / "Cargo.lock").write_text("")
b = rt.build_steps("rust", r)
check("rust build: release, locked", b[0].argv == ["cargo", "build", "--release", "--locked"] and b[0].timeout >= 3600)
check("rust build --bin", rt.build_steps("rust", r, "worker")[0].argv[-2:] == ["--bin", "worker"])
check("rust: built binary missing -> first start builds", rt.artifact_missing("rust", r, "")
      and any("build" in s.argv for s in rt.first_run_steps("rust", r)))
(r / "target/release").mkdir(parents=True)
(r / "target/release/api").write_text("")
check("rust: built binary there -> no build", not rt.artifact_missing("rust", r, ""))
(r / "Cargo.toml").write_text('[workspace]\nmembers = ["a", "b"]\n')
check("rust workspace without a binary named -> clear problem", "several binaries" in (rt.start_problem("rust", r, "") or ""))
g = WORK / "go"
g.mkdir()
(g / "go.mod").write_text("module x\n")
gb = rt.build_steps("go", g, "cmd/api")
check("go build into .venvy/bin/app", gb[0].argv[:4] == ["go", "build", "-trimpath", "-o"] and gb[0].argv[-1] == "./cmd/api")
j = WORK / "jv"
j.mkdir()
(j / "pom.xml").write_text("<project><dependencies><dependency><groupId>g</groupId><artifactId>a</artifactId><version>1</version></dependency></dependencies></project>")
check("maven build", rt.build_steps("java", j)[0].argv[0] == "mvn")
(j / "mvnw").write_text("")
check("maven wrapper preferred", rt.build_steps("java", j)[0].argv[:2] == ["sh", "./mvnw"])
(j / "target").mkdir()
(j / "target/app-1.0.jar").write_text("")
(j / "target/app-1.0-sources.jar").write_text("")
check("built jar found (not -sources)", rt.java_artifact(j, "").name == "app-1.0.jar")
check("pom dependencies listed", rt.list_packages("java", lambda n: (j / n).read_text() if (j / n).exists() else None)
      == [{"name": "g:a", "version": "1"}])
n = WORK / "nd"
n.mkdir()
(n / "package.json").write_text('{"scripts": {"build": "x", "start": "y"}}')
check("npm without lockfile: install", rt.sync_steps("node", n)[0].argv[:2] == ["npm", "install"])
(n / "package-lock.json").write_text("{}")
check("npm with lockfile: ci", rt.sync_steps("node", n)[0].argv[:2] == ["npm", "ci"])
(n / "pnpm-lock.yaml").write_text("")
check("pnpm lockfile: pnpm --frozen-lockfile", "pnpm" in rt.sync_steps("node", n)[0].argv and "--frozen-lockfile" in rt.sync_steps("node", n)[0].argv)
check("node build script runs", "build" in rt.build_steps("node", n)[0].argv)
py = WORK / "py"
py.mkdir()
(py / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["flask>=3", "gunicorn"]\n')
st = rt.python_steps(py, "/v")
check("pyproject dependencies installed", st and st[0].argv[-3:] == ["--", "flask>=3", "gunicorn"] and st[0].argv[:3] == ["/v/bin/python", "-m", "pip"], str(st))
(py / "requirements.txt").write_text("x")
check("requirements.txt wins over bare pyproject", "-r" in rt.python_steps(py, "/v")[0].argv)
if shutil.which("uv"):
    (py / "uv.lock").write_text("")
    s0 = rt.python_steps(py, "/v")[0]
    check("uv.lock -> uv sync into the venv", s0.argv[:2] == ["uv", "sync"] and ("UV_PROJECT_ENVIRONMENT", "/v") in s0.env)

app = SimpleNamespace(id=7, parent_app_id=None, container_port=3000, memory_limit_mb=256, cpu_limit_percent=50,
                      pids_limit=100, egress_policy="allow", stop_timeout=20, stop_signal="SIGINT",
                      start_command=None, app_dir=str(WORK / "app7"))
argv = rt.docker_run_argv(app, WORK, "nginx:1.27", 8001, "127.0.0.1")
check("docker run: published on the chosen host address", "127.0.0.1:8001:3000" in argv)
check("docker run: limits, no-new-privileges, data mount", "--memory" in argv and "--cpus" in argv and "--pids-limit" in argv
      and "no-new-privileges" in argv and any(a.endswith(":/data") for a in argv))
check("docker run: stop signal and timeout", "--stop-signal" in argv and "--stop-timeout" in argv)
check("docker run: nothing that weakens the host", not any(x in argv for x in ("--privileged", "--network=host", "--pid=host")))
check("docker run: env file, not env in argv", "--env-file" in argv and not any("=" in a and a.isupper() for a in argv))
check("docker image refs validated", raises(rt.clean_entrypoint, "docker", "--privileged") and raises(rt.clean_entrypoint, "docker", "a b"))

# ── commit status ───────────────────────────────────────────────────────────
from venvy.deploy import commit_status_request  # noqa: E402
u, h, bd = commit_status_request("https://github.com/o/r.git", "T", "abc", "success", "ok", "venvy/x")
check("GitHub status URL", u == "https://api.github.com/repos/o/r/statuses/abc" and h["Authorization"] == "Bearer T")
u, h, bd = commit_status_request("https://gitlab.example.com/g/sub/r.git", "T", "abc", "failure", "no", "c")
check("GitLab status URL + state", u.endswith("/api/v4/projects/g%2Fsub%2Fr/statuses/abc") and bd["state"] == "failed" and h["PRIVATE-TOKEN"] == "T")
u, h, bd = commit_status_request("https://git.example.org/o/r", "T", "abc", "pending", "x", "c")
check("Gitea status URL", u == "https://git.example.org/api/v1/repos/o/r/statuses/abc" and h["Authorization"] == "token T")

# ── toolchains against a fake nodejs.org ────────────────────────────────────
from venvy import toolchains  # noqa: E402
arch = toolchains._arch()
ver = "20.11.1"
inner = f"node-v{ver}-linux-{arch}"
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:xz") as tf:
    data = b"#!/bin/sh\necho v20.11.1\n"
    ti = tarfile.TarInfo(f"{inner}/bin/node")
    ti.size, ti.mode = len(data), 0o755
    tf.addfile(ti, io.BytesIO(data))
tarball = buf.getvalue()
name = f"{inner}.tar.xz"
good_sum = hashlib.sha256(tarball).hexdigest()
state = {"sum": good_sum}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/index.json":
            body = json.dumps([{"version": "v20.11.1", "files": [f"linux-{arch}"], "lts": "Iron"},
                               {"version": "v20.9.0", "files": [f"linux-{arch}"]},
                               {"version": "v22.1.0", "files": [f"linux-{arch}"]}]).encode()
        elif self.path == f"/v{ver}/SHASUMS256.txt":
            body = f"{state['sum']}  {name}\n".encode()
        elif self.path == f"/v{ver}/{name}":
            body = tarball
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
toolchains.DIST["node"] = f"http://127.0.0.1:{srv.server_address[1]}"
state["sum"] = "0" * 64
try:
    asyncio.run(toolchains.install("node", "20"))
    bad_refused = False
except toolchains.ToolchainError as exc:
    bad_refused = "Checksum" in str(exc)
check("toolchain with a bad checksum is not installed", bad_refused and not toolchains.installed("node"))
state["sum"] = good_sum
v = asyncio.run(toolchains.install("node", "20"))
check("node 20 -> newest 20.x installed", v == "20.11.1" and toolchains.installed("node") == ["20.11.1"], str(toolchains.installed("node")))
check("toolchain bin dir is usable", (toolchains.bin_dir("node", v) / "node").is_file())
nd = WORK / "nvm"
nd.mkdir()
(nd / ".nvmrc").write_text("v20\n")
check(".nvmrc read", toolchains.repo_version("node", nd) == "20")
(nd / ".nvmrc").unlink()
(nd / "package.json").write_text('{"engines": {"node": ">=18.0"}}')
check("engines.node read", toolchains.repo_version("node", nd) == "18")
(nd / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.82"\n')
check("rust-toolchain.toml read", toolchains.repo_version("rust", nd) == "1.82")
napp = SimpleNamespace(runtime="node", runtime_version="20")
check("app path gets the toolchain first", toolchains.path_dirs(napp, nd) == [str(toolchains.bin_dir("node", v))])
check("remove toolchain", toolchains.remove("node", v) and not toolchains.installed("node"))
srv.shutdown()

# ── systemd service properties ──────────────────────────────────────────────
from venvy import launcher  # noqa: E402
launcher._version = 255
sapp = SimpleNamespace(id=9, memory_limit_mb=128, cpu_limit_percent=25, pids_limit=64, io_weight=50, nofile_limit=4096,
                       sandbox=True, egress_policy="allowlist", egress_allow="10.0.0.0/8,1.1.1.1")
props = launcher.properties(sapp, ids=(1009, 1009), wd=Path("/x/app"), signal_name="SIGINT", grace=12,
                            rw_paths=["/x/app", "/x/state"], ro_paths=["/x/vol"], as_root=False)
want = ["User=1009", "MemoryMax=128M", "CPUQuota=25%", "TasksMax=64", "IOWeight=50", "LimitNOFILE=4096",
        "KillSignal=SIGINT", "TimeoutStopSec=12s", "NoNewPrivileges=yes", "ProtectSystem=strict", "PrivateTmp=yes",
        "ProtectProc=invisible", "IPAddressDeny=any", "IPAddressAllow=localhost", "IPAddressAllow=10.0.0.0/8",
        "BindPaths=/x/app", "ReadWritePaths=/x/state", "BindReadOnlyPaths=/x/vol", "StandardOutput=journal",
        "SystemCallFilter=@system-service"]
missing = [w for w in want if w not in props]
check("service has user, limits, signal, sandbox, egress", not missing, str(missing))
check("service hides Venvy's data", any(p.startswith("TemporaryFileSystem=") for p in props))
check("exit status is recorded as root", any(p.startswith("ExecStopPost=+") for p in props))
rprops = launcher.properties(sapp, ids=None, wd=Path("/x"), signal_name="SIGTERM", grace=5, rw_paths=[], ro_paths=[], as_root=True)
check("docker CLI service: no sandbox (needs the daemon)", "ProtectSystem=strict" not in rprops)
check("exit file parsing", True)
launcher.EXIT_DIR = WORK / "exit"
launcher.EXIT_DIR.mkdir()
(launcher.EXIT_DIR / launcher.unit_name(9)).write_text("exit-code exited 3\n")
c1 = launcher.exit_code(9)
(launcher.EXIT_DIR / launcher.unit_name(9)).write_text("signal killed TERM\n")
c2 = launcher.exit_code(9)
check("exit codes: exited 3, killed by TERM -> -15", (c1, c2) == (3, -15), str((c1, c2)))

# ── env file for exec_env.py ────────────────────────────────────────────────
ef = WORK / "envf.json"
ef.write_text(json.dumps({"A": "1", "B": "two words"}))
out = subprocess.run([sys.executable, "-I", str(Path(repo, "venvy/runtime_assets/exec_env.py")), "--file", str(ef),
                      sys.executable, "-c", "import os; print(os.environ['A'], os.environ['B'])"], capture_output=True, text=True)
check("exec_env --file applies and deletes the file", out.stdout.strip() == "1 two words" and not ef.exists(), out.stderr)

# ── venv relocation ─────────────────────────────────────────────────────────
vv = WORK / "stage" / "venv"
(vv / "bin").mkdir(parents=True)
(vv / "bin" / "gunicorn").write_text(f"#!{vv}/bin/python\nimport x\n")
sp = vv / "lib" / "python3.12" / "site-packages"
sp.mkdir(parents=True)
(sp / "x.pth").write_text(f"{WORK}/stage/src\n")
subprocess.run([sys.executable, "-I", str(Path(repo, "venvy/runtime_assets/relocate.py")), str(vv), str(WORK / "stage"), "/final"], check=True)
check("venv shebangs and .pth rewritten", (vv / "bin/gunicorn").read_text().startswith("#!/final/venv/bin/python")
      and (sp / "x.pth").read_text().strip() == "/final/src")

# ── consistent SQLite copies ────────────────────────────────────────────────
src = WORK / "sq"
src.mkdir()
db = sqlite3.connect(src / "app.db")
db.execute("PRAGMA journal_mode=WAL")
db.execute("create table t(x)")
db.executemany("insert into t values (?)", [(i,) for i in range(500)])
db.commit()                                     # left open: the rows are in the WAL
outdir = WORK / "sqout"
r = subprocess.run([sys.executable, "-I", str(Path(repo, "venvy/runtime_assets/app_archive.py")), "sqlite", str(src), str(outdir)],
                   capture_output=True, text=True)
copied = json.loads(r.stdout or "[]")
cnt = sqlite3.connect(outdir / "app.db").execute("select count(*) from t").fetchone()[0] if copied else -1
check("SQLite copy includes what is still in the WAL", copied == ["app.db"] and cnt == 500, f"{copied} {cnt} {r.stderr}")
r = subprocess.run([sys.executable, "-I", str(Path(repo, "venvy/runtime_assets/app_archive.py")), "archive", f"{src}=files"],
                   capture_output=True)
with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:gz") as tf:
    names = tf.getnames()
    data = tf.extractfile("files/app.db").read()
(WORK / "fromtar.db").write_bytes(data)
cnt2 = sqlite3.connect(WORK / "fromtar.db").execute("select count(*) from t").fetchone()[0]
check("app archive: database consistent, no -wal/-shm", cnt2 == 500 and not any(x.endswith(("-wal", "-shm")) for x in names), str(names))
db.close()

# ── DB-backed parts (a scratch database in the working directory) ───────────
from sqlmodel import Session  # noqa: E402
from venvy.db import create_db_and_tables, engine  # noqa: E402
from venvy.models import App, Route, ServiceBinding  # noqa: E402
create_db_and_tables()
with Session(engine) as s:
    a1 = App(user_id=1, name="A1", slug="a1", app_dir=str(WORK / "a1"), venv_path="", entrypoint="", port=9101,
             status="running", domain="a.example.com", bind_public=False)
    a2 = App(user_id=1, name="A2", slug="a2", app_dir=str(WORK / "a2"), venv_path="", entrypoint="", port=9102, status="running")
    s.add(a1); s.add(a2); s.commit(); s.refresh(a1); s.refresh(a2)
    rep = App(user_id=1, name="A1 r", slug="a1", app_dir=a1.app_dir, venv_path="", entrypoint="", port=9103, status="running",
              parent_app_id=a1.id, process_kind="replica")
    s.add(rep)
    s.add(Route(app_id=a2.id, hostname="a.example.com", path_prefix="/api", strip_prefix=True))
    s.add(Route(app_id=a2.id, hostname="*.wild.example.com"))
    s.add(Route(app_id=a1.id, hostname="www.example.com", kind="redirect", redirect_to="example.com"))
    svc = App(user_id=1, name="db", slug="db", app_dir=str(WORK / "db"), venv_path="", entrypoint="postgres:17", port=5544,
              status="running", service_template="postgres",
              env_json=__import__("venvy.crypto", fromlist=["x"])._encrypt_secret(json.dumps({"POSTGRES_USER": "app", "POSTGRES_PASSWORD": "p@ss", "POSTGRES_DB": "app"})))
    s.add(svc); s.commit(); s.refresh(svc)
    s.add(ServiceBinding(app_id=a2.id, service_app_id=svc.id))
    s.commit()
    ids = (a1.id, a2.id, svc.id)
    legacy = [App(user_id=1, name=n, slug=n, app_dir=str(WORK / n), venv_path="", entrypoint=ep, runtime=r, port=p)
              for n, r, ep, p in (("lr", "rust", "main.rs", 9201), ("lg", "go", "main.go", 9202),
                                  ("nr", "rust", "api", 9203), ("np", "python", "main.rs", 9204))]
    for x in legacy:
        s.add(x)
    s.commit()
    legacy_ids = [x.id for x in legacy]
create_db_and_tables()                           # the startup migration runs again, as after an update
with Session(engine) as s:
    eps = [s.get(App, i).entrypoint for i in legacy_ids]
check("pre-v5 main.rs / main.go entrypoints cleared, others kept", eps == ["", "", "api", "main.rs"], str(eps))

# A build before the start writes its output to the app's log while it runs
# (a cold cargo build takes minutes; the log must not sit still meanwhile).
from venvy import process  # noqa: E402
with Session(engine) as s:
    papp = s.get(App, legacy_ids[0])
(WORK / "lr").mkdir(exist_ok=True)
plog = WORK / "prestart.log"


async def _prestart_live():
    step = rt.Step(["sh", "-c", "echo compiling-one; sleep 1.5; echo compiling-two"], 30, "fake build", True)
    task = asyncio.create_task(process._run_prestart_step(papp, step, plog))
    await asyncio.sleep(0.8)
    mid = plog.read_text()
    await task
    return mid, plog.read_text()

mid, end = asyncio.run(_prestart_live())
check("build output reaches the log while it runs", "compiling-one" in mid and "compiling-two" not in mid, repr(mid))
check("... and all of it, once, before 'finished'",
      end.count("compiling-one") == 1 and end.count("compiling-two") == 1
      and end.index("compiling-two") < end.index("fake build finished"), repr(end))

# A container's /data must let the image's own user (postgres, mysql) pass
# through to the folder it made there as root; everyone else's stays private.
import stat as _stat  # noqa: E402
modes = {}
for r_, i_ in (("docker", 9301), ("compose", 9302), ("python", 9303)):
    probe = SimpleNamespace(id=i_, parent_app_id=None, runtime=r_)
    appspec.ensure_state_dirs(probe, None)
    modes[r_] = _stat.S_IMODE(appspec.data_dir(probe).stat().st_mode)
    (appspec.data_dir(probe)).chmod(0o700)            # an existing folder from before: put right on the next start
    appspec.ensure_state_dirs(probe, None)
    modes[r_ + " again"] = _stat.S_IMODE(appspec.data_dir(probe).stat().st_mode)
check("container data folder 0711, others 0700",
      modes == {"docker": 0o711, "docker again": 0o711, "compose": 0o711, "compose again": 0o711,
                "python": 0o700, "python again": 0o700}, str({k: oct(v) for k, v in modes.items()}))
from venvy import routing  # noqa: E402
routing._build()
m = routing.match_sync("a.example.com", "/")
check("domain routes to its app", m and m.app_id == ids[0])
m = routing.match_sync("a.example.com", "/api/users")
check("longest path prefix wins", m and m.app_id == ids[1] and m.strip)
check("prefix needs a path boundary", routing.match_sync("a.example.com", "/apix").app_id == ids[0])
check("wildcard host", (routing.match_sync("x.wild.example.com", "/") or SimpleNamespace(app_id=0)).app_id == ids[1]
      and routing.match_sync("wild.example.com", "/") is None)
check("redirect rule", routing.match_sync("www.example.com", "/").kind == "redirect")
ups = {routing.upstreams(ids[0])[0][1] for _ in range(4)}
check("replicas are taken in turn", ups == {9101, 9103}, str(ups))
from venvy import services  # noqa: E402
benv = services.binding_env(ids[1])
check("service binding gives DATABASE_URL", benv.get("DATABASE_URL") == "postgresql://app:p%40ss@127.0.0.1:5544/app"
      and benv.get("PGPASSWORD") == "p@ss", str(benv))
f = services.new_service_fields("redis")
check("redis service: private, password not in argv",
      f["bind_public"] is False and "$$REDIS_PASSWORD" in f["start_command"] and f["env"]["REDIS_PASSWORD"] not in f["start_command"])
from venvy import firewall  # noqa: E402
rules = firewall.ruleset()
check("firewall: private app's ports closed to the network", 'iifname != "lo" tcp dport { 9101, 9103 } drop' in rules, rules)
check("firewall: public app's port left open", "9102" not in rules.split("chain output")[0])
from venvy import edge, kv  # noqa: E402
kv.put_json(edge.CADDY_KEY, {"enabled": True, "email": "", "venvy_hosts": ["panel.example.com"]})
txt = edge.caddyfile_text(edge.caddy_settings())
check("Caddyfile: on-demand TLS asks Venvy, redirects to HTTPS", "on_demand_tls" in txt and "/_venvy/edge-ask" in txt
      and "redir https://{host}{uri} 308" in txt and "reverse_proxy 127.0.0.1:" in txt)
check("Caddy may certify routed names only", edge.ask("a.example.com") and edge.ask("x.wild.example.com")
      and edge.ask("panel.example.com") and not edge.ask("evil.example.net"))
pw = routing.hash_password("s3cret")
check("basic auth hashes verify", routing._check_password("s3cret", pw) and not routing._check_password("x", pw))
rule = routing.Rule(host="h", prefix="/", app_id=1, rate_limit=3, route_id=99, ip_allow=routing._networks("10.0.0.0/8"))
check("rate limit", [routing.rate_ok(rule, "1.2.3.4") for _ in range(4)] == [True, True, True, False])
check("ip allow-list", routing.ip_allowed(rule, "10.1.2.3") and not routing.ip_allowed(rule, "11.0.0.1")
      and routing.ip_allowed(rule, "::ffff:10.0.0.1"))
cr = routing.Rule(host="h", prefix="/", app_id=1, cache_seconds=60)
check("cache only public responses", routing.cache_ttl(cr, {"cache-control": "public, max-age=30"}) == 30
      and routing.cache_ttl(cr, {"cache-control": "private"}) == 0
      and routing.cache_ttl(cr, {"cache-control": "max-age=30", "set-cookie": "a"}) == 0)
from venvy import logship  # noqa: E402
check("log targets validated", raises(logship.clean_target, {"kind": "ftp"}) and raises(logship.clean_target, {"kind": "loki", "url": "ftp://x"}))
msg = logship._rfc5424(0, (5, "A", "web"), "hello")
check("syslog RFC 5424 line", msg.startswith(b"<14>1 1970-01-01T00:00:00") and msg.endswith(b"venvy-app-5 - web - hello"))
from venvy import telemetry  # noqa: E402
prom = telemetry.prometheus()
check("prometheus text has app gauges", 'venvy_app_up{app="A1"' in prom and "# TYPE venvy_http_requests_total counter" in prom)
check("otlp payload built from the same numbers", any(m["name"] == "venvy_app_up"
      for m in telemetry._otlp_payload()["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]))
from venvy import secrets_ext  # noqa: E402
check("dotenv parsing", secrets_ext._read_dotenv("A=1\nexport B='two'\n# c\nbad\n") == {"A": "1", "B": "two"})
check("vault path validated", secrets_ext.clean_vault_path("secret/app") == "secret/app" and raises(secrets_ext.clean_vault_path, "x"))
check("secret file names validated", raises(secrets_ext.clean_files, {"../x": "a"}))
from venvy import groups  # noqa: E402
with Session(engine) as s:
    parent = s.get(App, ids[1])
    parent.instances = 3
    s.add(parent); s.commit()
    groups.sync_replicas(s, parent)
    reps = groups.children(s, ids[1], "replica")
    res = groups.sync_workers(s, parent, {"web": "x", "worker": "celery", "beat": "celery beat"})
    workers = groups.children(s, ids[1], "worker")
    eff = groups.effective(workers[0], s)
check("instances=3 -> two replica rows", len(reps) == 2)
check("Procfile -> workers (web skipped)", sorted(w.process_name for w in workers) == ["beat", "worker"] and res["added"])
check("a worker runs with its app's settings and its own command", eff.app_dir == parent.app_dir and eff.start_command in ("celery", "celery beat")
      and eff.health_type == "none")

# ── paths a route is chosen with ────────────────────────────────────────────
n = routing.normalize_path
check("path normalised: escapes, dots, slashes",
      n("/%61dmin") == "/admin" and n("//admin") == "/admin" and n("/x/../admin/") == "/admin/"
      and n("/a/./b") == "/a/b" and n("/") == "/" and n("/%2e%2e/admin") == "/admin" and n("/caf%c3%a9") == "/caf%C3%A9")
routing._build()
check("an encoded path can't miss a path route", routing.match_sync("a.example.com", n("/%61pi/users")).app_id == ids[1]
      and routing.match_sync("a.example.com", n("//api")).app_id == ids[1])
check("path-routed hosts are path sensitive", routing.path_sensitive("a.example.com", routing.match_sync("a.example.com", "/"))
      and not routing.path_sensitive("x.wild.example.com", routing.match_sync("x.wild.example.com", "/")))
check("encoded slashes and backslashes are ambiguous", routing.ambiguous_path("/api%2F..%2Fx") and routing.ambiguous_path("/a\\b")
      and not routing.ambiguous_path("/api/x?q=%2F"))
with Session(engine) as s:
    check("hostname owners: domain and routes", routing.hostname_apps(s, "a.example.com") == {ids[0], ids[1]}
          and routing.hostname_apps(s, "nobody.example.com") == set())

# ── hostnames (no backtracking pattern) ─────────────────────────────────────
from venvy import own_hosts  # noqa: E402
vh = own_hosts.valid_hostname
check("hostnames checked label by label", vh("app.example.com") and vh("*.example.com", wildcard=True)
      and not vh("*.example.com") and not vh("example") and not vh("-a.example.com") and not vh("a..b.com")
      and not vh("0." * 3000 + "!"))
t0 = time.monotonic()
vh("0." * 120 + "0!", wildcard=True)
check("hostname check is quick on hostile input", time.monotonic() - t0 < 0.05)

# ── compose files ───────────────────────────────────────────────────────────
from venvy import compose_guard as cg  # noqa: E402
root = WORK / "capp"
(root / "sub").mkdir(parents=True)
roots = [Path(os.path.realpath(root))]


def refused(project):
    try:
        cg.check(project, roots)
        return False
    except cg.ComposeRefused:
        return True


ok_project = {"services": {"web": {"image": "nginx", "volumes": [{"type": "bind", "source": str(root / "sub"), "target": "/x"},
                                                                  {"type": "volume", "source": "d", "target": "/d"}],
                                   "build": {"context": str(root), "dockerfile": "Dockerfile"}, "cap_add": ["NET_BIND_SERVICE"],
                                   "security_opt": ["no-new-privileges:true"]}},
              "volumes": {"d": {}}}
check("a contained compose project passes", not refused(ok_project))
os.symlink("/etc", root / "etc-link")
bad = [
    {"services": {"x": {"privileged": True}}},
    {"services": {"x": {"cap_add": ["SYS_ADMIN"]}}},
    {"services": {"x": {"devices": ["/dev/sda:/dev/sda"]}}},
    {"services": {"x": {"network_mode": "host"}}},
    {"services": {"x": {"pid": "host"}}},
    {"services": {"x": {"ipc": "container:other"}}},
    {"services": {"x": {"security_opt": ["seccomp:unconfined"]}}},
    {"services": {"x": {"volumes": [{"type": "bind", "source": "/", "target": "/host"}]}}},
    {"services": {"x": {"volumes": [{"type": "bind", "source": str(root / "etc-link"), "target": "/e"}]}}},
    {"services": {"x": {"volumes": [{"type": "bind", "source": "/var/run/docker.sock", "target": "/s"}]}}},
    {"services": {"x": {"build": {"context": "/"}}}},
    {"services": {"x": {"build": {"context": str(root), "ssh": ["default"]}}}},
    {"services": {"x": {"build": {"context": str(root), "dockerfile": "/etc/shadow"}}}},
    {"services": {"x": {"env_file": [{"path": "/var/lib/venvy/.venvy_key"}]}}},
    {"services": {"x": {}}, "volumes": {"v": {"driver_opts": {"type": "none", "o": "bind", "device": "/etc"}}}},
    {"services": {"x": {}}, "volumes": {"v": {"external": True}}},
    {"services": {"x": {}}, "networks": {"n": {"driver": "macvlan"}}},
    {"services": {"x": {}}, "secrets": {"s": {"file": "/etc/shadow"}}},
]
check("compose escapes refused", all(refused(b) for b in bad), str([b for b in bad if not refused(b)]))

# ── toolchain archives on Pythons without tar filters ───────────────────────
from venvy import toolchains as tcx  # noqa: E402


def tar_with(*members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, kind, link in members:
            ti = tarfile.TarInfo(name)
            if kind == "sym":
                ti.type, ti.linkname = tarfile.SYMTYPE, link
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
            tf.addfile(ti, io.BytesIO(b"") if kind == "file" else None)
    buf.seek(0)
    return tarfile.open(fileobj=buf)


dest = WORK / "tdest"
dest.mkdir()


def unsafe(*members):
    try:
        tcx._checked_members(tar_with(*members), dest)
        return False
    except tcx.ToolchainError:
        return True


check("tar fallback: plain archive passes", not unsafe(("bin", "dir", ""), ("bin/node", "file", ""), ("lib", "sym", "bin")))
check("tar fallback: escapes refused", unsafe(("../x", "file", "")) and unsafe(("x", "sym", "../../..")) and unsafe(("y", "sym", "/etc"))
      and unsafe(("bin/z", "sym", "../../z")) and unsafe(("../" + dest.name + "2/x", "file", "")))

check("monitor targets: no metadata address, this host only for admins",
      telemetry.clean_target("https://example.com/health") and telemetry.clean_target("tcp://127.0.0.1:5432")
      and raises(telemetry.clean_target, "http://169.254.169.254/latest/meta-data/")
      and raises(telemetry.clean_target, "tcp://127.0.0.1:5432", local_ok=False)
      and raises(telemetry.clean_target, "http://localhost:8080/", local_ok=False)
      and raises(telemetry.clean_target, "tcp://[::ffff:127.0.0.1]:22", local_ok=False)
      and telemetry.clean_target("https://example.com/", local_ok=False))

shutil.rmtree(WORK, ignore_errors=True)
print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
