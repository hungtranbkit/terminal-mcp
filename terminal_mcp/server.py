from __future__ import annotations

from .mcp_app import build_mcp


# Kept as a module-level object for backward compatibility and SDK inspection.
mcp = build_mcp()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
