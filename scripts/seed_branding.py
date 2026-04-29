#!/usr/bin/env python3
"""Seed the Postgres `branding` table with a logo SVG for a given org.

Reads the logo from the specified path (default: haderach-home landing logo),
strips Illustrator metadata bloat, and upserts the clean SVG into the branding
row for the given org.

Usage:
    cd agent
    source .venv/bin/activate

    # Seed haderach (default)
    DATABASE_URL="postgresql://..." python scripts/seed_branding.py

    # Seed arcade with a custom logo
    DATABASE_URL="postgresql://..." python scripts/seed_branding.py --org arcade --logo /path/to/arcade-logo.svg
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(interpolate=False)

from service.pg_client import get_pool

DEFAULT_LOGO_PATH = Path(__file__).resolve().parent.parent.parent / "haderach-home" / "public" / "assets" / "landing" / "logo.svg"


def strip_svg(raw: str) -> str:
    """Strip Illustrator bloat from an SVG export.

    Removes:
      - <?xml ...?> processing instruction
      - <!-- Generator: Adobe Illustrator ... --> comments
      - <metadata>...</metadata> block (bulk of the file — embedded PGF)
      - Illustrator-specific namespace declarations (xmlns:i, xmlns:xlink)
      - id="Layer_1" and version="1.1" attributes
    """
    svg = re.sub(r"<\?xml[^?]*\?>\s*", "", raw)
    svg = re.sub(r"<!--.*?-->\s*", "", svg, flags=re.DOTALL)
    svg = re.sub(r"\s*<metadata>.*?</metadata>\s*", "", svg, flags=re.DOTALL)
    svg = re.sub(r'\s+xmlns:i="[^"]*"', "", svg)
    svg = re.sub(r'\s+xmlns:xlink="[^"]*"', "", svg)
    svg = re.sub(r'\s+id="Layer_1"', "", svg)
    svg = re.sub(r'\s+version="1\.1"', "", svg)
    # Collapse runs of whitespace but preserve newlines for readability
    svg = re.sub(r"[ \t]+\n", "\n", svg)
    svg = re.sub(r"\n{3,}", "\n\n", svg)
    return svg.strip() + "\n"


def main():
    parser = argparse.ArgumentParser(description="Seed branding for an org")
    parser.add_argument(
        "--org",
        default="haderach",
        help="Org slug to seed branding for (default: haderach)",
    )
    parser.add_argument(
        "--logo",
        type=Path,
        help="Path to logo SVG (default: haderach-home landing logo)",
    )
    args = parser.parse_args()

    logo_file = args.logo or DEFAULT_LOGO_PATH
    if not logo_file.exists():
        print(f"Error: logo file not found at {logo_file}")
        sys.exit(1)

    raw = logo_file.read_text(encoding="utf-8")
    svg_content = strip_svg(raw)
    print(f"Read logo SVG from {logo_file}")
    print(f"  Raw: {len(raw):,} chars  ->  Stripped: {len(svg_content):,} chars  ({100 - len(svg_content) * 100 // len(raw)}% reduction)")

    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO branding (org_slug, logo_svg, show_lockup)
               VALUES (%s, %s, 'none')
               ON CONFLICT (org_slug) DO UPDATE
                 SET logo_svg = EXCLUDED.logo_svg,
                     show_lockup = EXCLUDED.show_lockup""",
            (args.org, svg_content),
        )

    print(f"Upserted branding row for org={args.org} with cleaned logo SVG.")
    print("\nDone.")


if __name__ == "__main__":
    main()
