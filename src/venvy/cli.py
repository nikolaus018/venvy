"""Command-line entry point: ``venvy``."""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from venvy import __version__
from venvy.config import configure, get_settings
from venvy.logging_config import configure_logging

__all__ = ["build_parser", "main"]

_BANNER = r"""
{indigo}{bold}    ██╗   ██╗███████╗███╗   ██╗██╗   ██╗██╗   ██╗
    ██║   ██║██╔════╝████╗  ██║██║   ██║╚██╗ ██╔╝
    ██║   ██║█████╗  ██╔██╗ ██║██║   ██║ ╚████╔╝
    ╚██╗ ██╔╝██╔══╝  ██║╚██╗██║╚██╗ ██╔╝  ╚██╔╝
     ╚████╔╝ ███████╗██║ ╚████║ ╚████╔╝    ██║
      ╚═══╝  ╚══════╝╚═╝  ╚═══╝  ╚═══╝     ╚═╝   {reset}
    {gray}Self-hosted application manager — v{version}{reset}

    {indigo}➜{reset}  {bold}Local:{reset}    http://127.0.0.1:{port}
    {indigo}➜{reset}  {bold}Network:{reset}  http://{host}:{port}
    {indigo}➜{reset}  {bold}Data:{reset}     {data_dir}
    {gray}──────────────────────────────────────────────────{reset}
"""


#: Shown when the terminal cannot encode the block-drawing banner.
_PLAIN_BANNER = """
    Venvy v{version} — self-hosted application manager

    ->  Local:    http://127.0.0.1:{port}
    ->  Network:  http://{host}:{port}
    ->  Data:     {data_dir}
"""


def _print_banner(host: str, port: int, data_dir: Path) -> None:
    """Print the startup banner.

    Colour is dropped when stdout is not a terminal, and the whole thing falls
    back to ASCII if the console encoding cannot represent the block-drawing
    characters — a banner must never be the reason a server fails to start.
    """
    colour = sys.stdout.isatty()
    fields = {
        "indigo": "\033[38;5;63m" if colour else "",
        "gray": "\033[90m" if colour else "",
        "bold": "\033[1m" if colour else "",
        "reset": "\033[0m" if colour else "",
        "version": __version__,
        "host": host,
        "port": port,
        "data_dir": data_dir,
    }
    try:
        print(_BANNER.format(**fields))
    except UnicodeEncodeError:
        print(_PLAIN_BANNER.format(**fields))


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    defaults = get_settings()
    parser = argparse.ArgumentParser(
        prog="venvy",
        description="Venvy — self-hosted application manager.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=defaults.host, help="Address to bind to.")
    parser.add_argument("--port", type=int, default=defaults.port, help="Port to bind to.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=defaults.data_dir,
        help="Directory holding the database, apps and encryption key.",
    )
    parser.add_argument(
        "--log-level",
        default=defaults.log_level,
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Logging verbosity.",
    )
    parser.add_argument(
        "--reload", action="store_true", help="Reload on code changes (development only)."
    )
    parser.add_argument("--version", action="version", version=f"venvy {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the Venvy server. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    configure_logging(args.log_level)
    active = configure(
        dataclasses.replace(
            get_settings(),
            host=args.host,
            port=args.port,
            data_dir=args.data_dir.expanduser().resolve(),
            log_level=args.log_level,
        )
    )
    active.data_dir.mkdir(parents=True, exist_ok=True)

    # Imported here so that --help and --version do not pay for the whole
    # FastAPI and SQLModel import graph.
    import uvicorn

    from venvy.core.net import get_server_ipv4

    # Binding all interfaces is an explicit operator choice; the banner then
    # shows the LAN address rather than the unhelpful 0.0.0.0.
    wildcard = ("0.0.0.0", "::")  # noqa: S104
    display_host = get_server_ipv4() if args.host in wildcard else args.host
    _print_banner(display_host, args.port, active.data_dir)

    uvicorn.run(
        # An import string rather than the object, so --reload can re-import it.
        "venvy.app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=args.reload,
        # Venvy sits behind a Cloudflare Tunnel, which terminates TLS and adds
        # the forwarding headers uvicorn needs to report the right scheme.
        # Client IPs are resolved from CF-Connecting-IP rather than these — see
        # venvy.security.dependencies.resolve_client_ip.
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
