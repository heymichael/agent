#!/usr/bin/env python3
"""Seed the Postgres `branding` table with the current logo SVG.

Reads the logo from haderach-home/public/assets/landing/logo.svg, strips
Illustrator metadata bloat (the <metadata> block is ~170 KB of embedded
PGF data), and upserts the clean SVG into the singleton branding row.

Usage:
    cd agent
    source .venv/bin/activate
    DATABASE_URL="postgresql://..." python scripts/seed_branding.py [--dry-run]
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(interpolate=False)

from service.pg_client import get_pool

LOGO_PATH = Path(__file__).resolve().parent.parent.parent / "haderach-home" / "public" / "assets" / "landing" / "logo.svg"


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
    logo_file = LOGO_PATH
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
            """INSERT INTO branding (id, logo_svg, show_lockup)
               VALUES (1, %s, false)
               ON CONFLICT (id) DO UPDATE
                 SET logo_svg = EXCLUDED.logo_svg,
                     show_lockup = EXCLUDED.show_lockup""",
            (svg_content,),
        )

    print("Upserted branding row with cleaned logo SVG (show_lockup=false).")
    print("\nDone.")


if __name__ == "__main__":
    main()
