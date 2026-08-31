from __future__ import annotations

import sys

from . import __version__
from .mcp_app import build_mcp


# Kept as a module-level object for backward compatibility and SDK inspection.
mcp = build_mcp()


def main() -> None:
    if sys.argv[1:] == ["--version"]:
        print(__version__)
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
