"""Command-line interface: `cwscraper scan` / `cwscraper serve` / `cwscraper niches`."""
from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources

from cwscraper import __version__
from cwscraper.core.engine import ScanEngine
from cwscraper.core.niche import load_niche
from cwscraper.core.store import JSONRepository


def cmd_scan(args: argparse.Namespace) -> int:
    if args.niche:
        os.environ["CWSCRAPER_NICHE"] = args.niche
    repo = JSONRepository()
    niche = load_niche()
    engine = ScanEngine(repo, niche)
    print(f"Scanning with niche pack: {niche.display_name}")
    result = engine.run_full_scan()
    print(json.dumps(result, indent=2))
    return 0 if "error" not in result else 1


def cmd_serve(args: argparse.Namespace) -> int:
    if args.niche:
        os.environ["CWSCRAPER_NICHE"] = args.niche
    from cwscraper.web.app import app  # late import to honor env var

    port = args.port or int(os.getenv("CWSCRAPER_PORT", "5050"))
    host = args.host or os.getenv("CWSCRAPER_HOST", "0.0.0.0")
    print(f"\n  CheckWell Enterprise Scraper v{__version__}")
    print(f"  Dashboard: http://localhost:{port}\n")
    app.run(host=host, port=port, debug=args.debug)
    return 0


def cmd_niches(args: argparse.Namespace) -> int:
    pkg_files = resources.files("cwscraper.niches")
    for f in sorted(pkg_files.iterdir()):
        if f.name.endswith(".yaml"):
            slug = f.name.removesuffix(".yaml")
            try:
                niche = load_niche(slug)
                print(f"  {slug:<14} {niche.display_name}")
            except Exception as e:
                print(f"  {slug:<14} (load failed: {e})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cwscraper",
        description="CheckWell Enterprise Scraper — community lead engine.",
    )
    parser.add_argument("--version", action="version", version=f"cwscraper {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Run a one-shot scan and print results")
    p_scan.add_argument("--niche", help="Niche pack slug (default: $CWSCRAPER_NICHE or 'caregiver')")
    p_scan.set_defaults(func=cmd_scan)

    p_serve = sub.add_parser("serve", help="Run the Flask dashboard")
    p_serve.add_argument("--niche", help="Niche pack slug")
    p_serve.add_argument("--port", type=int, help="Port (default 5050)")
    p_serve.add_argument("--host", help="Host (default 0.0.0.0)")
    p_serve.add_argument("--debug", action="store_true", help="Flask debug mode")
    p_serve.set_defaults(func=cmd_serve)

    p_niches = sub.add_parser("niches", help="List bundled niche packs")
    p_niches.set_defaults(func=cmd_niches)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
