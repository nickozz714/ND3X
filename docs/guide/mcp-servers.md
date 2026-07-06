# MCP Servers

**MCP (Model Context Protocol) servers** are the things that *provide tools*. Connect a
server, discover its tools, and those tools become available to attach to skills. ND3X
talks to several at once and mixes their tools freely.

## The MCP Servers tab

Open **AI Workbench → MCP Servers**. You see the registered servers, their status, and a
detail view per server (where you discover/sync tools and configure auth).

## Server types

When you add a server you choose a **server type**:

| Type | Use it for | Key fields |
|------|-----------|------------|
| **http** | A remote MCP server reachable over HTTP | **Base URL** |
| **sse** | A remote MCP server using Server-Sent Events | **Base URL** |
| **stdio** | A local MCP server started as a subprocess | **Command** (+ optional **Install command**) |
| **builtin** | Platform-native tools | *Read-only — cannot be created/edited in the UI* |

## Adding an MCP server

1. **AI Workbench → MCP Servers → Add.**
2. Fill in:
   - **Name*** and **Slug*** (e.g. `My Service` / `my-service`).
   - **Description** (optional).
   - **Server Type*** — `http`, `sse`, or `stdio`.
   - For `http`/`sse`: **Base URL*** (`https://…`).
   - For `stdio`: **Command*** (e.g. `fabric-mcp`) and optionally an **Install command**
     (e.g. `pipx install fabric-mcp`) that runs once when the server is registered.
   - **Enabled** — leave on to use it.
3. **Save.** (Builtin servers cannot be created here — they ship with the platform.)
4. **Configure authentication** if the server needs it (in the server's detail view): API
   keys / headers / OAuth as the server requires.
5. **Discover tools** from the detail view — this pulls the server's published tools into
   the **Tools** tab. From there, enable the ones you want and attach them to skills.

> A disabled server keeps its registration but exposes no tools. Disabling is the safe way
> to "unplug" a provider without deleting it.

## MCP servers already in this platform

| Server | Role |
|--------|------|
| **Builtin** | Platform-native tools (text/document store, file inspection, PDF rendering, guarded shell, Azure login). Always present, read-only. |
| **ND3X MCP Server** | The platform's own service tools. |
| **Exa** | Public web research (search + fetch). |
| **Deep Wiki** | Documentation/wiki lookup. |
| **Context7** | Library/framework documentation lookup. |
| **Fabric MCP Server** | Microsoft Fabric / OneLake operations. |
| **Firecrawl** | *Disabled* — web crawling/extraction (not currently in use). |
| **PlayWright** | *Disabled* — browser automation (dropped; tools pruned). |

> If you remove or disable a server, remember to prune its now-**orphan tools** in the
> Tools tab — tools attached to no skill are dead weight and clutter selection.

## Health & availability

Some MCP services run as separate processes/containers and may be intermittently
unavailable. The platform degrades gracefully (a missing MCP service returns a clean
"unavailable" rather than crashing a turn) — a down server simply means its tools can't be
called until it's back.
