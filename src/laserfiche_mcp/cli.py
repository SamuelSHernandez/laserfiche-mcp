"""Command-line entrypoint: arg parsing, --diagnose probe, config-error formatting.

Lives outside ``server.py`` so the server module can stay focused on tool
registration. The ``laserfiche-mcp`` console script declared in
``pyproject.toml`` resolves to ``laserfiche_mcp.server:main``, which
delegates here.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from ._app import get_settings
from .auth import build_auth_strategy
from .client import LaserficheClient
from .config import Settings
from .errors import LaserficheError
from .observability import configure_logging

logger = logging.getLogger("laserfiche_mcp")

_HELP_TEXT = """laserfiche-mcp — Model Context Protocol server for Laserfiche, plus a CLI
that does the same work with no model in the loop.

Usage:
  laserfiche-mcp                Start the stdio MCP server (for local clients
                                that spawn it: Claude Desktop, Cursor, etc.).
  laserfiche-mcp serve          Same, stated explicitly.
  laserfiche-mcp --http         Serve over Streamable HTTP for web / cloud
                                clients (claude.ai and ChatGPT connectors).
  laserfiche-mcp setup          First-run wizard: asks for your server
                                details, saves them, verifies the connection.
  laserfiche-mcp --diagnose     Probe the configured server and print a
  laserfiche-mcp diagnose       deployment-fitness report (no MCP started).
                                Run this FIRST when anything misbehaves.
  laserfiche-mcp --help         Show this message.
  laserfiche-mcp --version      Print version and exit.

Repository commands (no LLM involved, no tokens spent):
  ls FOLDER                     List a folder's children.        (alias: list)
  get ENTRY --to PATH           Stream a document to a file.     (alias: download)
  cat ENTRY                     Print a document's text.         (alias: read)
  find ENTRY PATTERN            Search inside one document; prints page + excerpt.
  search QUERY                  Full-text search the repository, with excerpts.
  manifest FOLDER --out FILE    Walk a tree; write a CSV/JSONL inventory.
  dedupe FOLDER                 Find byte-identical documents.   (alias: duplicates)
  diff ENTRY_A ENTRY_B          Compare two entries.             (alias: compare)

ENTRY and FOLDER accept either a numeric entry ID or a repository path.
Every command takes --json for machine-readable output. Run any command
with --help for its own options.

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

Configuration: run `laserfiche-mcp setup` once, or set LF_* environment
variables / a .env file in the working directory. (Precedence: env vars,
then ./.env, then the file setup wrote.) Required at a minimum:

  LF_REPO_API_URL    Base URL of your Repository API Server
  LF_REPOSITORY_ID   Repository name or ID
  LF_USERNAME        Service account username
  LF_PASSWORD        Service account password

See https://github.com/SamuelSHernandez/laserfiche-mcp#configure for the
full list including OAuth, SSL, retry, write-mode safety guards, and
logging knobs.

With no subcommand this binary starts the MCP server on stdio, which is how
MCP clients (Claude Desktop, Claude Code, MCP Inspector) launch it. Running
it directly without env config is expected to exit with a configuration
error.
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


def _common_flags_parser() -> argparse.ArgumentParser:
    """Flags every subcommand accepts, in addition to the top-level parser.

    ``--config`` and the verbosity flags default to ``SUPPRESS`` here so that
    writing them *before* the subcommand still works: without SUPPRESS, the
    subparser would re-apply its own default and silently overwrite the value
    the top-level parser already parsed. ``--json`` has a real default because
    only the subparsers define it.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit machine-readable JSON on stdout instead of a text report.",
    )
    common.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="Load environment from this .env file instead of ./.env discovery.",
    )
    verbosity = common.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=argparse.SUPPRESS,
        help="Increase log verbosity (DEBUG).",
    )
    verbosity.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Decrease log verbosity (WARNING).",
    )
    return common


def _add_repository_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    """Register the subcommands that read the repository directly."""
    entry_help = "Numeric entry ID, or a repository path."

    p_ls = subparsers.add_parser(
        "ls", aliases=["list"], parents=[common], help="List a folder's children."
    )
    p_ls.add_argument("folder", help=entry_help)
    p_ls.add_argument("--limit", type=int, default=100, help="Page size (default 100).")
    p_ls.add_argument("--skip", type=int, default=0, help="Offset into the listing.")

    p_get = subparsers.add_parser(
        "get",
        aliases=["download"],
        parents=[common],
        help="Stream a document to a local file.",
    )
    p_get.add_argument("entry", help=entry_help)
    p_get.add_argument("--to", metavar="PATH", default=None, help="Destination file or directory.")
    p_get.add_argument("--force", action="store_true", help="Overwrite an existing file.")
    p_get.add_argument("--max-bytes", type=int, default=None, help="Refuse files above this size.")

    p_cat = subparsers.add_parser(
        "cat",
        aliases=["read"],
        parents=[common],
        help="Print a document's extracted text.",
    )
    p_cat.add_argument("entry", help=entry_help)
    p_cat.add_argument("--pages", default=None, help="Page selection, e.g. '4-9' or '1,3,5-7'.")
    p_cat.add_argument("--max-chars", type=int, default=0, help="Truncate output (0 = no limit).")
    p_cat.add_argument("--max-bytes", type=int, default=None, help="Refuse files above this size.")

    p_find = subparsers.add_parser("find", parents=[common], help="Search inside one document.")
    p_find.add_argument("entry", help=entry_help)
    p_find.add_argument("pattern", help="Text to find. Literal unless --regex is given.")
    p_find.add_argument("--regex", action="store_true", help="Treat the pattern as a regex.")
    p_find.add_argument("--case-sensitive", action="store_true", help="Match case exactly.")
    p_find.add_argument(
        "--context", type=int, default=200, help="Excerpt size in characters (default 200)."
    )
    p_find.add_argument(
        "--limit", type=int, default=50, help="Maximum matches to print (default 50)."
    )
    p_find.add_argument("--max-bytes", type=int, default=None, help="Refuse files above this size.")

    p_search = subparsers.add_parser(
        "search", parents=[common], help="Full-text search with matched passages."
    )
    p_search.add_argument("query", help="A phrase, or raw Laserfiche syntax if it starts with '{'.")
    p_search.add_argument("--folder", default=None, help="Restrict to this folder subtree.")
    p_search.add_argument(
        "--limit", type=int, default=25, help="Matching entries to return (default 25)."
    )
    p_search.add_argument(
        "--hits-for-top",
        type=int,
        default=5,
        help="Fetch passages for this many results (default 5).",
    )
    p_search.add_argument(
        "--hits-per-entry", type=int, default=3, help="Passages per result (default 3)."
    )
    p_search.add_argument(
        "--context", type=int, default=300, help="Passage size in characters (default 300)."
    )
    p_search.add_argument(
        "--timeout", type=float, default=60.0, help="Seconds to wait for the search (default 60)."
    )

    p_manifest = subparsers.add_parser(
        "manifest", parents=[common], help="Walk a tree and write an inventory."
    )
    p_manifest.add_argument("folder", help=entry_help)
    p_manifest.add_argument("--out", metavar="PATH", default=None, help="File to write.")
    p_manifest.add_argument(
        "--format", choices=("csv", "jsonl"), default="csv", help="Output format (default csv)."
    )
    p_manifest.add_argument(
        "--no-recursive", action="store_true", help="List only immediate children."
    )
    p_manifest.add_argument(
        "--max-entries",
        type=int,
        default=100_000,
        help="Stop after this many entries (default 100000).",
    )

    p_dedupe = subparsers.add_parser(
        "dedupe",
        aliases=["duplicates"],
        parents=[common],
        help="Find byte-identical documents.",
    )
    p_dedupe.add_argument("folder", help=entry_help)
    p_dedupe.add_argument(
        "--no-recursive", action="store_true", help="Only consider immediate children."
    )
    p_dedupe.add_argument(
        "--max-bytes", type=int, default=None, help="Skip documents above this size."
    )
    p_dedupe.add_argument(
        "--max-entries",
        type=int,
        default=100_000,
        help="Stop the walk after this many entries (default 100000).",
    )

    p_diff = subparsers.add_parser(
        "diff", aliases=["compare"], parents=[common], help="Compare two entries."
    )
    p_diff.add_argument("left", help=entry_help)
    p_diff.add_argument("right", help=entry_help)
    p_diff.add_argument(
        "--no-fields", action="store_true", help="Compare attributes only, not template fields."
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI args. Separated from ``main`` so it's testable in isolation.

    Subcommands are additive: a bare invocation still parses to
    ``command=None`` and starts the MCP server, which is how every existing
    MCP client config launches this binary.
    """
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

    common = _common_flags_parser()
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.add_parser("serve", parents=[common], help="Start the stdio MCP server.")
    subparsers.add_parser("diagnose", parents=[common], help="Print a deployment-fitness report.")
    subparsers.add_parser(
        "setup",
        help="Interactive first-run wizard: save connection details, then verify them.",
    )
    _add_repository_commands(subparsers, common)

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
        line(
            "Write tools allowed",
            settings.write_tools_allowed or "(none — all write tools registered)",
        )
        line("Delete batch cap", str(settings.delete_folder_max_descendants))
        line("Audit reason required", str(settings.require_audit_reason).lower())
        line("Validate required fields", str(settings.validate_required_fields).lower())


def _classify_first_probe_failure(
    exc: LaserficheError,
) -> tuple[str, list[str], bool]:
    """Interpret the first probe's failure for the diagnose report.

    Returns ``(headline, advice_lines, try_other_api_version)``. The old
    behavior blamed LF_USERNAME/LF_PASSWORD for *every* failure, which sent
    installers with a wrong URL off to reset service-account passwords.
    The failure classes are actually distinguishable:

    - no HTTP status at all → the server was never reached (URL, DNS,
      VPN/firewall, or TLS trust — not credentials);
    - 400/404 before auth even matters → usually the wrong LF_API_VERSION
      or a wrong base path, so we offer to probe the other version;
    - 401/403 (or LF error codes 9010/9528) → genuinely credentials.
      9528's server message claims LFDS is unreachable, but in practice
      it almost always means a bad username/password.
    """
    status = exc.status_code

    if status is None:
        return (
            "UNREACHABLE",
            [
                "The server was never reached — this is not a credentials problem.",
                "Check LF_REPO_API_URL (exact base URL incl. /LFRepositoryAPI),",
                "VPN/firewall, DNS, and — for self-signed certs — LF_VERIFY_SSL.",
            ],
            False,
        )

    if status in (400, 404):
        return (
            f"FAIL (HTTP {status})",
            [
                f"HTTP {status} on the first probe usually means the wrong",
                "LF_API_VERSION or base path, not bad credentials.",
            ],
            True,
        )

    if status in (401, 403):
        return (
            f"FAIL (HTTP {status})",
            [
                "Check LF_USERNAME / LF_PASSWORD and the service account's",
                "permissions. (Laserfiche error 9528 — 'LFDS unreachable' — "
                "usually also means bad credentials, despite its wording.)",
            ],
            False,
        )

    return (
        f"FAIL (HTTP {status})",
        [
            f"Unexpected failure: {exc}",
            "Check LF_USERNAME / LF_PASSWORD if this persists; a 5xx here "
            "usually means the Laserfiche server itself is unhealthy.",
        ],
        False,
    )


async def _probe_other_api_version(settings: Settings) -> str | None:
    """Try the first probe against the *other* API version.

    Returns the working version string (``"v1"``/``"v2"``) or None. Lets
    diagnose say "set LF_API_VERSION=v2" instead of leaving the installer
    to bisect it themselves.
    """
    from .config import ApiVersion  # noqa: PLC0415

    other = ApiVersion.V2 if settings.api_version is ApiVersion.V1 else ApiVersion.V1
    flipped = settings.model_copy(update={"api_version": other, "retry_attempts": 0})
    try:
        async with LaserficheClient(flipped, build_auth_strategy(flipped)) as client:
            await client.list_field_definitions(max_results=1)
    except LaserficheError:
        return None
    return other.value


async def _run_diagnose(settings: Settings) -> int:
    """Probe the configured server for endpoint availability.

    Prints a deployment-fitness report to stdout and exits with status 0 if
    auth works (regardless of endpoint variability) or 1 if auth itself
    fails. Designed for new adopters figuring out what their LF build
    actually supports.

    Probes run with ``retry_attempts=0``: the whole point of diagnose is a
    fast verdict, and the configured exponential backoff (up to 10 retries)
    would turn an unreachable server into minutes of silence before the
    UNREACHABLE line ever printed.
    """
    settings = settings.model_copy(update={"retry_attempts": 0})
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
            headline, advice, try_other = _classify_first_probe_failure(exc)
            line("Authentication", headline)
            print()
            for advice_line in advice:
                print(f"  {advice_line}")
            if try_other:
                working = await _probe_other_api_version(settings)
                if working is not None:
                    print(
                        f"\n  The other API version responded. Set "
                        f"LF_API_VERSION={working} and re-run diagnose."
                    )
                else:
                    print(
                        "\n  The other API version failed too — recheck "
                        "LF_REPO_API_URL and LF_REPOSITORY_ID."
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


# Where `laserfiche-mcp setup` writes its config, and where every later
# invocation looks as a last resort. Home-anchored so a records manager can
# run the CLI from any directory once setup has been completed.
USER_ENV_PATH = Path.home() / ".laserfiche-mcp" / ".env"


def _load_user_env_if_unconfigured() -> None:
    """Fall back to the setup wizard's config file when nothing else is set.

    Precedence stays: real env vars > ./.env > the user-level file. We only
    load the user-level file when the other two sources are absent, because
    ``load_dotenv`` writes into ``os.environ`` — loading it unconditionally
    would let it override a project-local ``.env``.
    """
    if os.environ.get("LF_REPO_API_URL") or os.path.isfile(".env"):
        return
    if USER_ENV_PATH.is_file():
        from dotenv import load_dotenv  # noqa: PLC0415

        load_dotenv(USER_ENV_PATH, override=False)


def _url_problem(url: str) -> str | None:
    """Return a human-readable objection to a server URL, or None if usable.

    Catches the two mistakes the wizard actually sees: a bare hostname
    (``lf.example.org``) and a pasted UNC/local path. Anything with an
    http(s) scheme and a host passes — deeper validation happens when the
    connection check runs.
    """
    from urllib.parse import urlsplit  # noqa: PLC0415

    try:
        parts = urlsplit(url)
    except ValueError:
        return "That doesn't look like a URL."
    if parts.scheme not in ("http", "https"):
        return "The URL must start with http:// or https:// (e.g. https://lf.example.org/LFRepositoryAPI)."
    if not parts.netloc:
        return "The URL has no host name. Expected something like https://lf.example.org/LFRepositoryAPI."
    return None


def _env_quote(value: str) -> str:
    """Quote a value for a ``.env`` file when it needs it.

    An unquoted dotenv value ends at an inline ``#`` comment and has its
    surrounding whitespace stripped, so a password like ``p4$s word#1``
    would silently round-trip wrong. Double-quoting with backslash escapes
    matches what python-dotenv (and pydantic-settings) parse back out.

    One sequence has NO working escape: ``${...}`` is interpolated by
    python-dotenv in every quoting style, so values containing it cannot
    be stored in a .env file at all — the setup wizard rejects them up
    front (see ``_dotenv_unstorable``).
    """
    import re  # noqa: PLC0415

    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dotenv_unstorable(value: str) -> str | None:
    """Explain why ``value`` cannot round-trip through a .env file, or None.

    python-dotenv expands ``${VAR}`` references in every quoting style and
    offers no escape for them, so a credential containing ``${`` would load
    back corrupted. Refusing up front beats writing a config that fails
    with a wrong-password error later.
    """
    if "${" in value:
        return (
            "Values containing '${' cannot be stored in a .env file — the "
            "loader expands ${VAR} references and there is no escape. Set "
            "this credential directly as an environment variable instead, "
            "or use a different value."
        )
    return None


def _prompt(label: str, *, default: str | None = None, secret: bool = False) -> str:
    """One wizard prompt. Empty input takes the default when there is one."""
    suffix = f" [{default}]" if default else ""
    while True:
        if secret:
            import getpass  # noqa: PLC0415

            raw = getpass.getpass(f"  {label}{suffix}: ")
        else:
            raw = input(f"  {label}{suffix}: ")
        value = raw.strip() or (default or "")
        if value:
            return value
        print("    A value is required.")


def _run_setup() -> int:
    """Interactive first-run wizard: ask, write the user-level .env, verify.

    Exists for the person who was handed a terminal but not a lesson on
    environment variables. Deliberately runs BEFORE settings validation —
    its whole purpose is the case where no valid config exists yet.
    """
    print("laserfiche-mcp setup — connect this machine to your Laserfiche server.")
    print(f"Answers are saved to {USER_ENV_PATH} (only readable by you).")
    print("Ask your Laserfiche administrator for these values if unsure.\n")

    try:
        if USER_ENV_PATH.is_file():
            answer = input(
                f"  A saved configuration already exists at {USER_ENV_PATH}.\n"
                "  Overwrite it? [y/N]: "
            )
            if answer.strip().lower() not in ("y", "yes"):
                print("Keeping the existing configuration — nothing was changed.")
                return 0
        while True:
            url = _prompt(
                "Repository API URL (e.g. https://lf.example.org/LFRepositoryAPI)",
            )
            problem = _url_problem(url)
            if problem is None:
                break
            print(f"    {problem}")
        repo = _prompt("Repository name or ID")
        username = _prompt("Service account username")
        while True:
            password = _prompt("Service account password", secret=True)
            objection = _dotenv_unstorable(password)
            if objection is None:
                break
            print(f"    {objection}")
        # Normalize and validate BEFORE anything is written: "V1" or "2"
        # would otherwise persist an invalid LF_API_VERSION that breaks
        # every later invocation.
        while True:
            raw_version = _prompt("API version (v1 unless told otherwise)", default="v1")
            api_version = "v" + raw_version.strip().lower().lstrip("v")
            if api_version in ("v1", "v2"):
                break
            print(f"    {raw_version!r} is not a version — enter v1 or v2.")
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled — nothing was written.")
        return 1

    USER_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_ENV_PATH.write_text(
        "# Written by `laserfiche-mcp setup`. Re-run setup to change these.\n"
        f"LF_REPO_API_URL={_env_quote(url)}\n"
        f"LF_REPOSITORY_ID={_env_quote(repo)}\n"
        f"LF_USERNAME={_env_quote(username)}\n"
        f"LF_PASSWORD={_env_quote(password)}\n"
        f"LF_API_VERSION={_env_quote(api_version)}\n",
        encoding="utf-8",
    )
    # Best-effort tightening; a no-op on filesystems without POSIX modes.
    with contextlib.suppress(OSError):
        USER_ENV_PATH.chmod(0o600)

    print("\nSaved. Checking the connection...\n")
    try:
        from pydantic import SecretStr  # noqa: PLC0415

        settings = Settings(
            repo_api_url=url,  # type: ignore[arg-type]
            repository_id=repo,
            username=username,
            password=SecretStr(password),
            api_version=api_version,  # type: ignore[arg-type]
        )
    except (ValidationError, ValueError) as exc:
        print(_format_config_error(exc), file=sys.stderr)
        print("\nFix the values by re-running: laserfiche-mcp setup", file=sys.stderr)
        return 1

    exit_code = asyncio.run(_run_diagnose(settings))
    if exit_code == 0:
        print(
            "\nYou're connected. Try:\n"
            "  laserfiche-mcp list 1              (browse the repository root)\n"
            '  laserfiche-mcp search "a phrase"   (find documents by contents)'
        )
    else:
        print(
            "\nThe connection check failed — see the advice above, then "
            "re-run: laserfiche-mcp setup",
            file=sys.stderr,
        )
    return exit_code


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

    if getattr(args, "command", None) == "setup":
        sys.exit(_run_setup())

    _load_user_env_if_unconfigured()

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

    command = getattr(args, "command", None)

    if args.diagnose or command == "diagnose":
        exit_code = asyncio.run(_run_diagnose(settings))
        sys.exit(exit_code)

    if command is not None and command != "serve":
        # Repository subcommands never start the MCP server, and never
        # register write tools — they are read-only by construction.
        from .cli_commands import run as run_command  # noqa: PLC0415

        sys.exit(run_command(settings, args))

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
