"""Keeping docker compose apps inside their own boundaries.

`docker compose` talks to the Docker daemon, which runs as root: a compose
file that asks for a privileged container, the host's network or PID
namespace, a device, or a bind mount of / is root on the host for whoever
wrote it. So before Venvy runs compose for an app (build, pull, up), it:

1. renders the app's compose file with `docker compose config` — as the
   app's user, so `env_file`, `include` and `extends` can only read what the
   app itself can read;
2. checks the rendered project against the rules below;
3. runs compose on that rendered copy, which only Venvy can write, so the
   file the daemon gets is the one that was checked, whatever the app's
   files say a moment later.

Refused: privileged containers and builds, added capabilities beyond
Docker's defaults, devices and GPUs, the host's (or another container's)
network / PID / IPC / UTS / user / cgroup namespaces, cgroup parents,
security options other than no-new-privileges, other OCI runtimes,
external volumes, networks, configs, secrets and links, volume drivers or
driver options (which can bind any host path), network drivers other than
bridge, SSH-agent forwarding into builds, build entitlements, and any host
path — bind mounts, build contexts, Dockerfiles, env files, config and
secret files — outside the app's folder, its $APP_DATA_DIR and the volumes
attached to it.
"""
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from venvy.config import DATA_DIR

RENDER_DIR = DATA_DIR / ".compose"

# Docker's default capability set: asking for one of these again changes nothing.
_DEFAULT_CAPS = {"AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "MKNOD", "NET_BIND_SERVICE",
                 "NET_RAW", "SETFCAP", "SETGID", "SETPCAP", "SETUID", "SYS_CHROOT"}
_NS_KEYS = ("network_mode", "pid", "ipc", "uts", "userns_mode", "cgroup")
_REMOTE_CONTEXT = ("http://", "https://", "git://", "git@", "ssh://", "docker-image://", "oci-layout://",
                   "service:", "target:")


class ComposeRefused(ValueError):
    pass


def _roots(app) -> List[Path]:
    from venvy import appspec, volumes
    roots = [Path(app.app_dir), appspec.data_dir(app)]
    try:
        roots += [p for p, _ro in volumes.mounts(appspec.owner_id(app))]
    except Exception:
        pass
    out = []
    for r in roots:
        try:
            out.append(Path(os.path.realpath(r)))
        except OSError:
            continue
    return out


def _inside(path: str, roots: Iterable[Path]) -> bool:
    if not path or not os.path.isabs(path):
        return False
    real = Path(os.path.realpath(path))
    return any(real == r or r in real.parents for r in roots)


def check(project: dict, roots: List[Path]) -> None:
    """Raise ComposeRefused if the rendered *project* reaches outside the app."""
    def no(what: str) -> None:
        raise ComposeRefused(f"The compose file is not allowed here: {what}.")

    def host_path(p, what: str) -> None:
        if not isinstance(p, str) or not _inside(p, roots):
            no(f"{what} {p!r} is outside the app's folder, its data folder and its volumes")

    if not isinstance(project, dict):
        no("it did not render to a project")
    for key in ("volumes", "networks", "configs", "secrets"):
        for name, spec in (project.get(key) or {}).items():
            spec = spec or {}
            if spec.get("external"):
                no(f"{key[:-1]} '{name}' is external (it belongs to something else on this host)")
            if key == "volumes" and (spec.get("driver") not in (None, "", "local") or spec.get("driver_opts")):
                no(f"volume '{name}' sets a driver or driver options")
            if key == "networks" and spec.get("driver") not in (None, "", "bridge"):
                no(f"network '{name}' uses the {spec.get('driver')} driver")
            if key in ("configs", "secrets") and spec.get("file"):
                host_path(spec["file"], f"{key[:-1]} '{name}' file")
    for name, svc in (project.get("services") or {}).items():
        svc = svc or {}
        if svc.get("privileged"):
            no(f"service '{name}' is privileged")
        extra = {str(c).upper().removeprefix("CAP_") for c in (svc.get("cap_add") or [])} - _DEFAULT_CAPS
        if extra:
            no(f"service '{name}' adds capabilities ({', '.join(sorted(extra))})")
        if svc.get("devices") or svc.get("device_cgroup_rules") or svc.get("gpus"):
            no(f"service '{name}' uses host devices")
        for res in ((svc.get("deploy") or {}).get("resources") or {}).values():
            if isinstance(res, dict) and res.get("devices"):
                no(f"service '{name}' reserves host devices")
        for k in _NS_KEYS:
            v = str(svc.get(k) or "")
            if v == "host" or v.startswith("container:"):
                no(f"service '{name}' sets {k}: {v}")
        if svc.get("cgroup_parent"):
            no(f"service '{name}' sets cgroup_parent")
        for opt in svc.get("security_opt") or []:
            if str(opt).replace("=", ":").split(":")[0].strip() != "no-new-privileges":
                no(f"service '{name}' sets security_opt {opt!r}")
        if svc.get("runtime") not in (None, "", "runc"):
            no(f"service '{name}' uses the {svc.get('runtime')} runtime")
        if svc.get("external_links"):
            no(f"service '{name}' links to containers outside the project")
        for vf in svc.get("volumes_from") or []:
            if str(vf).startswith("container:"):
                no(f"service '{name}' takes volumes from another container")
        for vol in svc.get("volumes") or []:
            if isinstance(vol, str):
                no(f"service '{name}' has a volume the renderer did not resolve")
            t = vol.get("type")
            if t == "bind":
                host_path(vol.get("source"), f"service '{name}' bind mount")
            elif t not in ("volume", "tmpfs"):
                no(f"service '{name}' has a {t} mount")
        for ef in svc.get("env_file") or []:
            host_path(ef.get("path") if isinstance(ef, dict) else ef, f"service '{name}' env_file")
        b = svc.get("build")
        if b:
            if isinstance(b, str):
                b = {"context": b}
            if b.get("privileged"):
                no(f"service '{name}' builds privileged")
            if b.get("entitlements"):
                no(f"service '{name}' asks for build entitlements")
            if b.get("ssh"):
                no(f"service '{name}' forwards SSH into its build")
            if str(b.get("network") or "") == "host" or str(b.get("network") or "").startswith("container:"):
                no(f"service '{name}' builds on the {b.get('network')} network")
            ctx = b.get("context") or ""
            if not str(ctx).startswith(_REMOTE_CONTEXT):
                host_path(ctx, f"service '{name}' build context")
                df = b.get("dockerfile")
                if df and not b.get("dockerfile_inline"):
                    host_path(df if os.path.isabs(df) else os.path.join(ctx, df), f"service '{name}' Dockerfile")
            for cname, cpath in (b.get("additional_contexts") or {}).items():
                if not str(cpath).startswith(_REMOTE_CONTEXT):
                    host_path(cpath, f"service '{name}' build context '{cname}'")


def _render(app, compose_file: str, project_dir: str, project: str, env: Optional[Dict[str, str]]) -> dict:
    """`docker compose config` for the app's file, run as the app's user."""
    from venvy import appspec, runtimes
    from venvy.isolation import (_as_app_user, _clean_root_env, _exec_env_argv, _isolation_active,
                                 _prepare_exec_env)
    argv = ["docker", "compose", "-p", project, "-f", compose_file, "--project-directory", project_dir,
            "config", "--format", "json"]
    run_env = {k: v for k, v in (env or {}).items()}
    run_env.update(PATH=runtimes.tool_search_path(), HOME=str(appspec.home_dir(app)), LANG="C.UTF-8")
    run_env["DOCKER_CONFIG"] = str(appspec.home_dir(app) / ".docker")
    env_tf = None
    try:
        if _isolation_active():
            env_tf, env_fd = _prepare_exec_env(run_env)
            full = _as_app_user(app, _exec_env_argv(env_fd, argv))
            r = subprocess.run(full, capture_output=True, timeout=120, env=_clean_root_env(), pass_fds=(env_fd,),
                               cwd="/")
        else:
            r = subprocess.run(argv, capture_output=True, timeout=120, env=run_env, cwd="/")
    finally:
        if env_tf is not None:
            env_tf.close()
    if r.returncode != 0:
        raise ComposeRefused("docker compose could not read the compose file: "
                             + r.stderr.decode(errors="replace").strip()[-500:])
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise ComposeRefused("docker compose config did not answer with JSON.")


def pinned_argv(app, argv: List[str], env: Optional[Dict[str, str]] = None) -> List[str]:
    """*argv* (docker compose -p NAME -f FILE --project-directory DIR …) with
    FILE replaced by a checked, Venvy-owned rendering of it. Raises
    ComposeRefused. Blocking."""
    if argv[:2] != ["docker", "compose"] or "-f" not in argv or "--project-directory" not in argv:
        return list(argv)
    project = argv[argv.index("-p") + 1] if "-p" in argv else f"venvy-app-{app.id}"
    f = argv[argv.index("-f") + 1]
    wd = argv[argv.index("--project-directory") + 1]
    if env is None:
        env = {}
        try:
            from venvy.process import _build_app_env
            env = _build_app_env(app)
        except Exception as exc:
            logging.debug("compose render without the app's variables: %s", exc)
    data = _render(app, f, wd, project, env)
    check(data, _roots(app))
    RENDER_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(RENDER_DIR, 0o700)
    out = RENDER_DIR / f"{project}.json"
    tmp = RENDER_DIR / f".{project}.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, out)
    pinned = list(argv)
    pinned[pinned.index("-f") + 1] = str(out)
    return pinned


def rendered_file(project: str) -> Optional[Path]:
    p = RENDER_DIR / f"{project}.json"
    return p if p.is_file() else None


def forget(project: str) -> None:
    p = RENDER_DIR / f"{project}.json"
    try:
        p.unlink()
    except OSError:
        pass
