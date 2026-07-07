# ND3X Platform — User & Operator Guide

This guide explains how to **use and operate** the ND3X platform: the AI agent, how to
build skills and tools, how to connect MCP servers, how to design workflows, how to
configure AI models for cost or performance, and what every tile in the desktop does.

For the **developer/internals** view (turn flow, contracts, code citations) see
[`../PLATFORM.md`](../PLATFORM.md). This guide is the day-to-day, screen-by-screen
companion to that reference.

> Custom domain apps — the **Project Management** and **Fitness Coach** (Lifestyle)
> tiles — are bespoke and intentionally **not** documented here; this guide covers the
> reusable *platform*.

---

## The desktop

ND3X presents as a windowed desktop. Each capability is a **tile** (a draggable,
resizable window) opened from the taskbar / launcher. Tiles fall into three groups:

- **Work surfaces** — Chat, Meeting, Server Files, PDF Viewer, Document Editor, Define Function.
- **Operations & observability** — Workflow Runs, Transfers, Audit Dashboard, System Logs,
  System Cognition, Productivity.
- **Administration** — **AI Workbench** (the hub below), KeyVault, Mail Settings,
  Notification Recipients, Application Settings, PDF Templates, Shell Console.

Each tile is described in [platform-tiles.md](platform-tiles.md).

## The AI Workbench

The **AI Workbench** tile (`Bot` icon) is where the agent itself is configured. It has
these tabs:

| Tab | What it manages | Guide |
|-----|-----------------|-------|
| **Agent Settings** | The single agent: its instruction + which skills are on | [agent.md](agent.md) |
| **Skills** | Reusable, use-case-scoped capabilities (description + instructions + tools) | [skills.md](skills.md) |
| **Tools** | The individual callable functions skills use | [tools.md](tools.md) |
| **MCP Servers** | Connecting tool providers (HTTP/SSE/stdio/builtin) | [mcp-servers.md](mcp-servers.md) |
| **Workflows** | Multi-step background pipelines | [workflows.md](workflows.md) |
| **AI Models** *(admin)* | Providers, local models, and per-capability routing | [ai-models.md](ai-models.md) |
| **Fabric Data Agents** *(admin)* | Query Microsoft Fabric Data Agents in natural language | [fabric-data-agents.md](fabric-data-agents.md) |
| **Builtins & System** *(admin)* | Enable/disable built-in tools and system/runtime skills | [builtins-system.md](builtins-system.md) |
| **Meeting Profiles** | How meetings are turned into notes | [meeting-profiles.md](meeting-profiles.md) |
| **Slash Commands** | Built-in and custom `/` commands for the chat composer | [slash-commands.md](slash-commands.md) |
| **Usage** | Token usage, budgets, cost breakdowns | [usage.md](usage.md) |
| **Users** *(admin)* | Accounts and roles | [users.md](users.md) |

## Roles

Three roles gate what you can see and do:

- **User** — use the agent, chat, run workflows, read most tiles.
- **Expert** — additionally manage skills, tools, MCP servers, and system-level skills.
- **Admin** — everything, including **AI Models** and **Users**.

## How it fits together (one sentence)

> One **agent** reads your message, picks the relevant **skill(s)** by their description,
> loads each skill's **tools** (which come from **MCP servers**), executes, and answers —
> and the same agent can be scripted into **workflows**; **AI Models** decides which LLM
> runs each step, and **Usage** tracks what it all costs.
