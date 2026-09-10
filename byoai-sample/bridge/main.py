#!/usr/bin/env python3
"""
main — bridge server entry point: argparse, logging, SSL, plugin discovery, asyncio loop

Role:
    Bootstraps the bridge. Parses CLI args (with env-var fallbacks for
    JWT keys), configures logging (root logger plus a dedicated message
    logger for protocol exchanges), validates SSL paths, constructs the
    BridgeServer, discovers and registers service plugins (Echo loopback
    + bot dispatcher), and runs the asyncio event loop until normal
    termination or operator-initiated shutdown (SIGINT).

Does not own:
    Wire-protocol handling (owned by bridge_server.py).
    Provider routing by botId prefix (owned by bot_service.py).
    Provider implementations (owned by providers/*).
    JWT signature verification logic (owned by BridgeServer.check_auth
    and its handshake-time peer in bridge_server.py; main.py only wires
    the keys through).

Dependencies:
    bridge_server.BridgeServer: the core server class constructed here
        and run via server.start_server().
    Each plugin's register(server) entry point, invoked dynamically via
    _load_plugin from a file path (plugins live outside the standard
    Python import path).

RCMS lifecycle:
    Phase 1 (Start): not directly involved — main.py finishes booting
        before any session.start arrives.
    Phase 2 (During): not directly involved.
    Phase 3 (Closure): not directly involved.

    main.py is bootstrap-only. All RCMS protocol handling happens after
    asyncio.run(server.start_server()) hands control to BridgeServer.

Spec:
    RCMS spec §Security — JWT primary/secondary key rotation. The
        --jwt-primary-key / --jwt-secondary-key arguments (and their
        INFINITY_JWT_PRIMARY_KEY / INFINITY_JWT_SECONDARY_KEY env-var
        defaults) implement the spec's documented rotation contract:
        keys are tried in order so a deployment can rotate the primary
        without dropping in-flight authentications still using the
        previous key.
    RCMS spec §Media Encoding Options — the --codec choices
        (L16, PCMU, PCMA, G722) are exactly the spec's documented
        codec set.
    https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

    See bridge/schema/rcms.schema.json for the authoritative wire shape
    and bridge/schema/rcms.schema.md for behavioral notes.

See also:
    BUILDERS_GUIDE.md §4 — Bridge Configuration: CLI and env-var contract
    BUILDERS_GUIDE.md §6 — Avaya Infinity: Security Keys & JWT
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import os
import sys
from pathlib import Path

# Add the bridge/ directory and the project root to sys.path so the
# 'import bridge_server' below resolves and so the dynamic plugin
# imports under providers/* work without proper packaging. Acceptable
# for a single-process server; a packaged distribution would replace
# this with console_scripts entry points and standard imports.
_BRIDGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _BRIDGE_DIR.parent
for _p in (_BRIDGE_DIR, _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import bridge_server  # noqa: E402 — sys.path insert above must precede this import

logger = logging.getLogger(__name__)


def _load_plugin(server: "bridge_server.BridgeServer", module_path: Path, module_name: str) -> None:
    """
    Load a service plugin from a Python file path and call its
    register(server) entry point so the plugin attaches itself to the
    bridge's service registry.

    The bridge uses a file-path-based plugin loader (rather than standard
    Python imports) because plugins live in directories outside the
    import path (bridge/ and providers/<provider>/). The loader registers
    each plugin under a dotted name in sys.modules so cross-plugin imports
    (e.g. providers/<other>/bot_<other>.py importing helpers from a
    sibling) resolve correctly.

    Args:
        server: the BridgeServer instance the plugin will register with.
        module_path: filesystem path to the plugin's main .py file
            (e.g. providers/echo/bot_echo.py).
        module_name: dotted-name registration key for sys.modules
            (e.g. "providers.echo.bot_echo"). Must match the package
            structure callers expect when importing plugin internals.

    Returns:
        None. Side effect: the plugin's register(server) attaches the
        plugin to server.service_registry.

    Raises:
        FileNotFoundError: module_path does not point at an existing
            file.
        RuntimeError: importlib could not build a valid spec for the
            module (corrupt file, unsupported file extension).
        AttributeError: the loaded module does not expose a top-level
            register() function. Every plugin must provide one.
    """
    if not module_path.is_file():
        raise FileNotFoundError(f"Plugin not found: {module_path}")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Invalid spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    # Explicitly set __name__ so the module's identity matches the
    # dotted name we register in sys.modules. Without this, importlib
    # uses the spec-derived name, which can differ from the registry
    # key and confuse downstream code that inspects module.__name__.
    module.__name__ = module_name
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    if hasattr(module, "register"):
        module.register(server)
        logger.info("Registered plugin: %s", module_path.name)
    else:
        raise AttributeError(f"{module_path} has no register function")


def main() -> None:
    """
    Boot the bridge server end-to-end and run the WSS event loop.

    Five-phase boot order:
        1. Argparse — parse CLI args; JWT key args fall back to
           INFINITY_JWT_PRIMARY_KEY / INFINITY_JWT_SECONDARY_KEY env vars.
           Fail fast via parser.error if --enable-auth is set without a
           primary key.
        2. Logging setup — rotate prior log files, create log directories
           if needed, configure the root logger and a dedicated 'message'
           logger that captures protocol message exchanges to a separate
           file.
        3. SSL validation — resolve relative cert / key paths against
           the bridge directory; abort with sys.exit on missing files.
        4. BridgeServer construction — pass host/port/ssl/auth/codec/JWT
           configuration to the server. The constructor itself raises
           ValueError if enable_auth=True and jwt_primary_key is empty
           (defense-in-depth alongside the parser.error guard at step 1).
        5. Plugin discovery and asyncio.run — load Echo and bot_service
           plugins via _load_plugin, then run server.start_server() in
           the asyncio event loop until normal return or SIGINT.

    Spec:
        RCMS spec §Security — JWT primary/secondary key rotation. The
        --jwt-primary-key / --jwt-secondary-key arguments (and their
        INFINITY_JWT_PRIMARY_KEY / INFINITY_JWT_SECONDARY_KEY env-var
        defaults) implement the spec's rotation contract.

    Args:
        None. CLI arguments are read from sys.argv via argparse.

    Returns:
        None. Process exit codes:
            0 — normal termination (server.start_server() returns) or
                operator-initiated shutdown via SIGINT (Ctrl-C).
            1 — SSL certificate or private-key file missing at
                start-server time (caught from start_server).
            2 — argparse failure (bad CLI args, or --enable-auth set
                without --jwt-primary-key / INFINITY_JWT_PRIMARY_KEY).

    See also:
        BUILDERS_GUIDE.md §4 — Bridge Configuration: CLI and env-var contract
        BUILDERS_GUIDE.md §6 — Avaya Infinity: Security Keys & JWT
    """
    parser = argparse.ArgumentParser(
        description=(
            "RCMS bridge server: accepts WebSocket connections from Avaya "
            "Infinity, routes bot.start by botId prefix to the configured "
            "AI provider, and manages the full call lifecycle per the RCMS "
            "spec."
        )
    )
    # Default 0.0.0.0 binds all interfaces, which assumes a TLS-terminating
    # reverse proxy (Caddy / nginx) is fronting the bridge. For direct
    # exposure without a proxy, run with --host 127.0.0.1 and route
    # through your own proxy, or front the bridge with TLS via
    # --ssl-cert / --ssl-key.
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    # 8443 is the conventional alternative-HTTPS port; not spec-mandated.
    parser.add_argument("--port", type=int, default=8443, help="Bind port (default: 8443)")
    parser.add_argument(
        "--ssl-cert",
        type=str,
        default=None,
        help="Path to SSL certificate (required for WSS)",
    )
    parser.add_argument(
        "--ssl-key",
        type=str,
        default=None,
        help="Path to SSL private key (required for WSS)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    # Default log paths are relative to the current working directory at
    # launch time. Run from the bridge install root (or pass absolute
    # paths via --log-file / --message-log-file) for predictable log
    # placement across deployments.
    parser.add_argument(
        "--log-file",
        type=str,
        default="logs/bridge_log.txt",
        help="Debug log file (default: logs/bridge_log.txt, relative to cwd)",
    )
    parser.add_argument(
        "--message-log-file",
        type=str,
        default="logs/bridge_msg.txt",
        help="Protocol message exchange log file (default: logs/bridge_msg.txt, relative to cwd)",
    )
    parser.add_argument(
        "--enable-auth",
        action="store_true",
        help="Require JWT Bearer token in Authorization header",
    )
    parser.add_argument(
        "--jwt-primary-key",
        type=str,
        default=os.environ.get("INFINITY_JWT_PRIMARY_KEY", ""),
        help=(
            "Primary JWT signing key for token verification. Defaults to "
            "INFINITY_JWT_PRIMARY_KEY env var. Required when --enable-auth is set."
        ),
    )
    parser.add_argument(
        "--jwt-secondary-key",
        type=str,
        default=os.environ.get("INFINITY_JWT_SECONDARY_KEY", ""),
        help=(
            "Secondary JWT signing key for rotation. Defaults to "
            "INFINITY_JWT_SECONDARY_KEY env var. Optional — used as a "
            "fallback when the primary key fails verification, supporting "
            "key rotation without downtime per RCMS spec §Security."
        ),
    )
    parser.add_argument(
        "--codec",
        choices=["L16", "PCMU", "PCMA", "G722"],
        default="G722",
        help=(
            "Preferred audio codec for the bridge↔Infinity wire (default: G722). "
            "G.722 is wideband 16 kHz at 64 kbps and matches the native rate of "
            "ElevenLabs and Gemini's audio output, eliminating an intermediate "
            "resample. L16 is narrowband 8 kHz at 128 kbps. Selection is from "
            "Infinity's offered list at session.start; if the chosen codec is "
            "not offered, the first offered codec is used."
        ),
    )
    args = parser.parse_args()

    # JWT auth requires a primary key. Fail fast at startup rather than
    # silently allowing a missing key — see RCMS spec §Security.
    if args.enable_auth and not args.jwt_primary_key:
        parser.error(
            "--enable-auth requires --jwt-primary-key (or INFINITY_JWT_PRIMARY_KEY env var)"
        )

    # Rotate existing log files: rename *.txt to *.txt.bak at startup
    for path in (args.log_file, args.message_log_file):
        if os.path.isfile(path):
            bak = path + ".bak"
            try:
                os.replace(path, bak)
                print(f"Rotated log: {path} -> {bak}")
            except OSError as e:
                print(f"Warning: could not rotate {path}: {e}", file=sys.stderr)

    for path in (args.log_file, args.message_log_file):
        log_dir = os.path.dirname(path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
            print(f"Created log directory: {log_dir}")

    log_level = logging.DEBUG if args.verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()

    file_handler = logging.FileHandler(args.log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    message_log = logging.getLogger("message")
    message_log.setLevel(logging.INFO)
    message_log.propagate = False
    message_log.handlers.clear()
    msg_file_handler = logging.FileHandler(args.message_log_file, mode="a", encoding="utf-8")
    msg_file_handler.setLevel(logging.INFO)
    msg_file_handler.setFormatter(formatter)
    message_log.addHandler(msg_file_handler)
    # Inject the message logger as a module-level attribute on
    # bridge_server. bridge_server.py reads message_logger directly
    # from its module namespace rather than receiving it as a
    # constructor argument — dependency-injection-via-globals.
    # Future cleanup: pass message_log to the BridgeServer constructor.
    bridge_server.message_logger = message_log
    message_log.info("=" * 80)
    message_log.info("Message logger initialized - protocol message exchanges will be logged here")
    message_log.info("=" * 80)

    ssl_cert = args.ssl_cert
    ssl_key = args.ssl_key
    if ssl_cert and not os.path.isabs(ssl_cert):
        ssl_cert = str(_BRIDGE_DIR / ssl_cert)
    if ssl_key and not os.path.isabs(ssl_key):
        ssl_key = str(_BRIDGE_DIR / ssl_key)
    if ssl_cert and not os.path.isfile(ssl_cert):
        sys.exit(f"SSL cert not found: {ssl_cert}")
    if ssl_key and not os.path.isfile(ssl_key):
        sys.exit(f"SSL key not found: {ssl_key}")

    server = bridge_server.BridgeServer(
        host=args.host,
        port=args.port,
        ssl_cert=ssl_cert,
        ssl_key=ssl_key,
        enable_auth=args.enable_auth,
        # Binary transport over base64. RCMS spec §Media Encoding Options
        # supports both; binary is lower overhead per frame and is the
        # encoding observed in every production Infinity deployment we
        # have wire data for. The bridge can be reconfigured to base64
        # by changing this constructor argument; no Infinity-side change
        # is required because transport is negotiated at session.start.
        preferred_transport="binary",
        preferred_codec=args.codec,
        jwt_primary_key=args.jwt_primary_key,
        jwt_secondary_key=args.jwt_secondary_key,
    )

    # Plugin discovery: only Echo and bot_service are explicitly loaded
    # here. Echo is the loopback validation provider; bot_service is the
    # dispatcher that routes bot.start by botId prefix to AI provider
    # plugins (elevenlabs:, gemini:, openai:, xai:). Adding a new AI
    # provider does NOT mean adding a third _load_plugin call here —
    # AI providers are wired into bot_service.py's __init__ and routed
    # by botId prefix at session time. See bot_service.py for the
    # registration pattern and BUILDERS_GUIDE.md §4.
    _load_plugin(server, _PROJECT_ROOT / "providers" / "echo" / "bot_echo.py", "providers.echo.bot_echo")
    _load_plugin(server, _BRIDGE_DIR / "bot_service.py", "bridge.bot_service")

    logger.info("RCMS Bridge starting (WSS=%s)", bool(args.ssl_cert and args.ssl_key))
    try:
        asyncio.run(server.start_server())
    except FileNotFoundError as e:
        path = getattr(e, "filename", None) or str(e)
        print(f"Missing certificate or key file: {path}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Server stopped")


if __name__ == "__main__":
    main()
