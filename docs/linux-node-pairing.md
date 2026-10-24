# Linux node pairing

The Nodes page's **Add Node → Linux** flow issues a short-lived, single-use
enrollment code after an authorized dashboard action. The returned command
downloads a generic installer over trusted HTTPS and passes the code only to
that local process. The installer checks Linux/systemd, required tools, the
trusted Terminal MCP HTTPS checkout, and a connected Tailscale IPv4 address
before redeeming the code.

The controller returns the node bearer credential only in the HTTPS consume
response. The installer writes it atomically to `~/terminal-mcp/node-agent.token`
with mode `0600`, then uses the existing Linux node-agent installer and its
user systemd service. The node binds its agent to the detected Tailscale
address. It does not configure or expose SSH. Linger is enabled by the existing
installer when possible; otherwise it prints the one `sudo loginctl` action
needed to keep the user service running after logout.

For an existing Linux node, **Repair connection** fills in the registered node
ID and requires a checked approval before generating a repair code. A repair
code is tied to the registered hostname. Successful redemption replaces the
managed node credential and the existing registry row; it does not delete
sessions. The node service restarts with `KillMode=process`, preserving the
tmux server and its sessions. The dashboard reports Online only after its
existing heartbeat and execution health checks succeed.

Linux pairing requires `nodes.onboarding.bootstrap_origin` (or the selected
controller URL) to be a trusted HTTPS origin. The public bootstrap ingress
allowlist exposes only the secret-free Linux installer script in addition to
the existing exact enrollment paths. Invalid, expired, consumed, revoked, and
hostname-mismatched codes produce closed diagnostic codes; neither the code
nor the bearer token is written to application logs.

The Linux flow is the first platform implementation. The Add Node wizard keeps
Windows on its current onboarding flow; macOS remains unavailable in the UI.
