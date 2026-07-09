"""Command-line entrypoint: arg parsing, --diagnose probe, config-error formatting.

Lives outside ``server.py`` so the server module can stay focused on tool
registration. The ``laserfiche-mcp`` console script declared in
``pyproject.toml`` resolves to ``laserfiche_mcp.server:main``, which
delegates here.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import ValidationError

from . import __version__
from ._app import get_settings
from .auth import build_auth_strategy
from .client import LaserficheClient
from .config import Settings
from .errors import LaserficheError
from .observability import configure_logging

logger = logging.getLogger("laserfiche_mcp")

_HELP_TEXT = """\
laserfiche-mcp — Model Context Protocol server for Laserfiche.

Usage:
  laserfiche-mcp                Start the stdio MCP server (for local clients
                                that spawn it: Claude Desktop, Cursor, etc.).
  laserfiche-mcp --http         Serve over Streamable HTTP for web / cloud
                                clients (claude.ai and ChatGPT connectors).
  laserfiche-mcp --diagnose     Probe the configured server and print a
                                deployment-fitness report (no MCP started).
  laserfiche-mcp --help         Show this message.
  laserfiche-mcp --version      Print version and exit.

Options:
  --http                        Run the Streamable HTTP transport instead of
                                stdio. Binds to LF_HTTP_HOST:LF_HTTP_PORT
                                (default 127.0.0.1:8000, path /mcp). Loopback
                                by default; see --host / --port to override.
  --host HOST                   Override LF_HTTP_HOST for this run (--http only).
  --port PORT                   Override LF_HTTP_PORT for this run (--http only).
  -v, --verbose                 Increase log verbosity (DEBUG). Repeats are
                                accepted but have no further effect.
  -q, --quiet                   Decrease log verbosity (WARNING). Mutually
                                exclusive with --verbose.
  --config PATH                 Load environment from a specific .env file
                                instead of the default $CWD/.env discovery.

Exposing --http to a network requires LF_HTTP_AUTH_TOKEN (a bearer token
checked on every request) and TLS terminated by a reverse proxy in front.
See https://github.com/SamuelSHernandez/laserfiche-mcp#remote-http.

Configuration is read from LF_* environment variables (or a .env file in
the working directory). Required at a minimum:

  LF_REPO_API_URL    Base URL of your Repository API Server
  LF_REPOSITORY_ID   Repository name or ID
  LF_USERNAME        Service account username
  LF_PASSWORD        Service account password

See https://github.com/SamuelSHernandez/laserfiche-mcp#configure for the
full list including OAuth, SSL, retry, write-mode safety guards, and
logging knobs.

This binary is meant to be launched by an MCP client (Claude Desktop,
Claude Code, MCP Inspector). Running it directly without env config is
expected to exit with a configuration error.
"""


def _format_config_error(exc: Exception) -> str:
    """Convert a Pydantic ValidationError into a user-facing message."""
    lines = [
        "laserfiche-mcp: configuration is missing or invalid.",
        "",
    ]
    if isinstance(exc, ValidationError):
        for err in exc.errors():
            msg = err.get("msg", "")
            # Pydantic prefixes value_error.* messages with "Value error, "
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, ") :]
            lines.append(f"  - {msg}")
    else:
        lines.append(f"  - {exc}")
    lines.extend(
        [
            "",
            "Quick start:",
            "  1. Copy .env.example to .env and fill in your repository details, OR",
            "  2. Set LF_REPO_API_URL, LF_REPOSITORY_ID, LF_USERNAME, LF_PASSWORD",
            "     as environment variables (e.g. via your MCP client's `env` block).",
            "",
            "Docs: https://github.com/SamuelSHernandez/laserfiche-mcp#configure",
        ]
    )
    return "\n".join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI args. Separated from ``main`` so it's testable in isolation."""
    parser = argparse.ArgumentParser(
        prog="laserfiche-mcp",
        description="Model Context Protocol server for Laserfiche.",
        add_help=False,  # We render our own --help so the layout matches docs.
    )
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("-V", "--version", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--host", metavar="HOST", default=None)
    parser.add_argument("--port", metavar="PORT", type=int, default=None)
    parser.add_argument("--config", metavar="PATH", default=None)
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="count", default=0)
    verbosity.add_argument("-q", "--quiet", action="store_true")
    return parser.parse_args(argv)


def _resolve_log_level(settings: Settings, args: argparse.Namespace) -> str:
    """Settings.log_level is the default; ``--verbose`` / ``--quiet`` override."""
    if args.verbose:
        return "DEBUG"
    if args.quiet:
        return "WARNING"
    return settings.log_level.upper()


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one ``--diagnose`` endpoint probe.

    ``ok=True`` means the call returned without raising; the endpoint is
    available on this build. ``ok=False`` carries the HTTP status code (or
    ``?`` if the failure happened before a response arrived) so the report
    can show ``unavailable (HTTP 404)`` etc.
    """

    label: str
    ok: bool
    status_code: int | None = None

    def display_status(self) -> str:
        if self.ok:
            return "OK"
        code = self.status_code if self.status_code is not None else "?"
        return f"unavailable (HTTP {code})"


async def _run_probe(label: str, awaitable: Awaitable[object]) -> ProbeResult:
    """Await ``awaitable``; classify success or ``LaserficheError`` failure."""
    try:
        await awaitable
    except LaserficheError as exc:
        return ProbeResult(label=label, ok=False, status_code=exc.status_code)
    return ProbeResult(label=label, ok=True)


async def _probe_optional_endpoints(client: LaserficheClient) -> list[ProbeResult]:
    """Run the optional (non-fatal) endpoint probes used by ``--diagnose``.

    Returns the results in order so the caller can render them however it
    likes. Failures of any individual probe don't abort the sequence —
    the whole point of ``--diagnose`` is to map the build's surface.
    """
    probes: list[tuple[str, Awaitable[object]]] = [
        ("List repositories", client.list_repositories()),
        ("Field definitions", client.list_field_definitions(max_results=1)),
        ("Template definitions", client.list_template_definitions(max_results=1)),
        ("Tag definitions", client.list_tag_definitions(max_results=1)),
        ("Link definitions", client.list_link_definitions(max_results=1)),
        ("Audit reasons", client.get_audit_reasons()),
        ("Root folder children", client.list_folder(1, max_results=1)),
        (
            'SimpleSearches ({LF:Name="*"})',
            client.search_entries('{LF:Name="*"}', max_results=1),
        ),
    ]
    return [await _run_probe(label, aw) for label, aw in probes]


def _print_write_mode_report(settings: Settings) -> None:
    """Print the ``Write mode:`` section of the diagnostic report."""

    def line(label: str, status: str) -> None:
        print(f"  {label:<32} {status}")

    print()
    print("Write mode:")
    line("LF_READ_ONLY", str(settings.read_only).lower())
    if not settings.read_only:
        line("Write paths allow", settings.write_paths_allow or "(none — writes unfenced)")
        line("Write paths deny", settings.write_paths_deny or "(none)")
        line("Write tools allowed", settings.write_tools_allowed or "(all 15 write tools)")
        line("Delete batch cap", str(settings.delete_folder_max_descendants))
        line("Audit reason required", str(settings.require_audit_reason).lower())
        line("Validate required fields", str(settings.validate_required_fields).lower())


async def _run_diagnose(settings: Settings) -> int:
    """Probe the configured server for endpoint availability.

    Prints a deployment-fitness report to stdout and exits with status 0 if
    auth works (regardless of endpoint variability) or 1 if auth itself
    fails. Designed for new adopters figuring out what their LF build
    actually supports.
    """
    auth = build_auth_strategy(settings)

    def line(label: str, status: str, detail: str = "") -> None:
        print(f"  {label:<32} {status}" + (f"  {detail}" if detail else ""))

    print(f"\nlaserfiche-mcp {__version__} — server diagnostic")
    print(
        f"  Target: {settings.repo_api_url}{settings.repository_id} "
        f"(API {settings.api_version.value})"
    )
    print(f"  Auth:   mode={settings.auth_mode.value}, user={settings.username or '(none)'}")
    print()
    print("Endpoint probes:")

    async with LaserficheClient(settings, auth) as client:
        try:
            await client.list_field_definitions(max_results=1)
            line("Authentication", "OK")
        except LaserficheError as exc:
            line("Authentication", "FAIL", f"HTTP {exc.status_code}: {exc}")
            print(
                "\nAuthentication failed. Check LF_USERNAME / LF_PASSWORD "
                "and the service account's permissions."
            )
            return 1

        for result in await _probe_optional_endpoints(client):
            line(result.label, result.display_status())

    _print_write_mode_report(settings)
    _print_observability_report(settings)

    print()
    print(
        "Done. If anything above failed, see "
        "https://github.com/SamuelSHernandez/laserfiche-mcp#errors "
        "for the relevant error slug."
    )
    return 0


def _print_observability_report(settings: Settings) -> None:
    """Print the ``Observability:`` section of the diagnostic report."""

    def line(label: str, status: str) -> None:
        print(f"  {label:<32} {status}")

    print()
    print("Observability:")
    line("LF_LOG_LEVEL", settings.log_level.upper())
    line("LF_LOG_FORMAT", settings.log_format.lower())
    line("Per-tool-call structured log", "enabled (tool_logger decorator)")
    line("Credential redaction", "enabled (observability.redact)")


def _load_config_file(path: str) -> None:
    """Populate the process environment from a ``--config`` .env file."""
    if not os.path.isfile(path):
        print(
            f"laserfiche-mcp: --config file not found: {path}",
            file=sys.stderr,
        )
        sys.exit(2)
    from dotenv import load_dotenv

    # ``override=False`` lets existing env vars win over .env values —
    # matching the standard pydantic-settings precedence.
    load_dotenv(path, override=False)


def main(register_writes: Callable[[], None]) -> None:
    """Console-script entrypoint.

    ``register_writes`` is a callback the server module supplies — it
    consults ``LF_READ_ONLY`` and the write-tool allowlist before
    deciding which write tools to register. Threading it as a callback
    (rather than importing the server module directly) keeps the
    import graph one-way: server depends on cli, never the reverse.
    """
    args = _parse_args(sys.argv[1:])

    if args.help:
        print(_HELP_TEXT)
        return
    if args.version:
        print(f"laserfiche-mcp {__version__}")
        return

    if args.config is not None:
        _load_config_file(args.config)

    try:
        settings = get_settings()
    except (ValidationError, ValueError) as exc:
        print(_format_config_error(exc), file=sys.stderr)
        sys.exit(2)
    except NotImplementedError as exc:
        # Cloud mode and api_key auth raise this from the validator.
        print(f"laserfiche-mcp: {exc}", file=sys.stderr)
        sys.exit(2)

    log_level = _resolve_log_level(settings, args)
    configure_logging(level=log_level, format_=settings.log_format)

    if args.diagnose:
        exit_code = asyncio.run(_run_diagnose(settings))
        sys.exit(exit_code)

    register_writes()

    if args.http:
        # CLI overrides win over LF_HTTP_* env for this run.
        if args.host is not None:
            settings.http_host = args.host
        if args.port is not None:
            settings.http_port = args.port
        from .http_transport import run_http  # noqa: PLC0415

        run_http(settings)
        return

    # Import the FastMCP instance lazily to keep cli.py decoupled from the
    # tool-registration side effects in server.py.
    from .server import mcp  # noqa: PLC0415

    try:
        mcp.run()  # stdio transport by default
    except KeyboardInterrupt:
        # Don't dump a traceback for ordinary Ctrl-C exits.
        logger.info("laserfiche-mcp stopped.")
