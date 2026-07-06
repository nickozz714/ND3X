# Tools

A **tool** is a single callable function the agent can invoke — search documents, render a
PDF, list projects, run a shell command, call an HTTP endpoint, etc. Tools are the
lowest-level building block: **skills group tools**, and the agent calls tools while
executing a selected skill.

Every tool belongs to an **MCP server** (its provider). You usually don't write tools by
hand — you connect an MCP server and **discover** its tools ([mcp-servers.md](mcp-servers.md)),
then enable the ones you want and attach them to skills.

## The Tools tab

Open **AI Workbench → Tools**. Each tool record has:

| Field | Meaning |
|-------|---------|
| **Server*** | The MCP server that exposes it. |
| **Remote name*** | The tool's name as the server publishes it. |
| **Name*** | The local name the agent sees (e.g. `search_documents`). |
| **Description*** | What the tool does — helps the agent decide *which* tool within a skill. |
| **Type*** | Tool category/transport. |
| **Argument** (JSON) | The input schema / argument template. |
| **Output schema** | Optional shape of the result. |
| **Availability scope** | Optional gating of where the tool may be used. |
| **Enabled** | Whether the tool is usable. |

The list supports **bulk enable/disable**, **export**, and **delete**, plus a detail view.

## Where tools come from

- **Builtin server** — platform-native tools that ship with ND3X: text/document management
  (`text__*`), file inspection (`file_*`, `json_inspect`, `csv_profile`, …), PDF rendering
  (`pdf__render`), guarded shell + Azure login (`system__shell_exec`, `system__az_login`),
  and more. These are read-only/managed by the platform.
- **External MCP servers** — anything you connect (web research, Microsoft Fabric, docs
  lookup, your own services). Their tools appear here after discovery.

## Guarded / mutating tools

Tools that **change state or are destructive** are guarded: when the agent wants to call
one, it pauses and asks you to **confirm** first. Shell execution in particular is always
backend-guarded and requires confirmation. This is enforced by the platform, not by the
prompt — so even a confused agent can't silently mutate or delete.

## How to think about tools vs skills

- **Tool** = a single capability (one function).
- **Skill** = a use-case that bundles the right tools + instructions on how to use them.
- The agent never selects a *tool* directly from the catalog; it selects a **skill**, and
  the skill's attached tools become callable. So: **attach a tool to a skill** to make it
  reachable, and write the skill's instructions to tell the agent when to use each tool.

## Good practice

- Keep tool **descriptions** accurate and specific — within a selected skill, the agent
  chooses between tools by description.
- Disable tools you're not using rather than leaving a sprawling catalog (orphan tools —
  attached to no skill — are dead weight; prune or attach them).
- When you remove/replace an MCP server, clean up its now-orphan tools.
