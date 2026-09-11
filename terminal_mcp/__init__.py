"""Terminal MCP."""

# Bumped for the Finish Gate. 0.12.0 shipped before the contract
# handshake, default-open access, session provenance and the dashboard
# rework; every node on this fleet still runs it. Leaving both sides
# reporting the same string made `version` useless as a drift signal --
# only contract_version could tell an upgraded controller from a node that
# had not moved. Now the two disagree visibly, which is the point.
__version__ = "0.13.0"
