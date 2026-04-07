"""Allow running as ``python -m mcp_server``."""

from dotenv import load_dotenv

load_dotenv(interpolate=False)

from .server import main  # noqa: E402

main()
