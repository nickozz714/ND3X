# Fabric Data Agents

Connect one or more **Microsoft Fabric Data Agents** so the agent can answer
natural-language questions about your Fabric data (lakehouse / warehouse / semantic
model). Once connected, the agent can query them through a builtin tool and return
the grounded answer plus the query steps the Data Agent ran.

## Adding an agent
Under **AI Workbench → Fabric Data Agents** (admin), add an agent with its Fabric
workspace / data-agent details and choose how it authenticates:

- **Service principal** — a client id/secret + tenant (best for servers/headless).
- **Azure CLI session** — reuse an existing `az login` session on the host.
- **Bearer token** — a stored token (encrypted at rest).
- **Interactive browser** — a browser sign-in (**desktop app only** — needs the
  backend and browser on the same machine).

> The Data Agent must be **published** in Fabric — querying uses the published stage.

## Using it
Enabled agents are offered to the agent automatically; in chat you can just ask a
data question and it will query the right one. Auth credentials are stored
**encrypted** and are never returned or exported.
