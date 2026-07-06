# Platform Tiles

Beyond the **AI Workbench**, the desktop exposes a set of tiles. Each is a movable,
resizable window opened from the taskbar/launcher. This page covers the reusable platform
tiles.

> The **Project Management** and **Fitness Coach** tiles are custom domain apps and are
> intentionally out of scope here.

## Work surfaces

### Chat
The primary way to talk to the agent. Type a request; the agent selects skill(s), shows its
**plan**, runs tools (asking you to confirm anything mutating), and answers. Supports an
inline **document explorer** (chat with your docs), a **Live Voice** button (full-duplex,
if a realtime model is assigned — see [ai-models.md](ai-models.md)), and a model picker for
the turn.

### Meeting (Recording)
A capture surface for audio. Record or upload a meeting/voice memo; it's transcribed (via
the **Recordings → text** slot) and turned into notes. Offers capture **modes** (e.g.
*Meeting* vs *Requirements*) with live audio-level feedback. Needs a transcription model
assigned.

### Server Files
Browse the server's file system. Used to pick files to add as **context** for the agent
(e.g. code chat) and to inspect generated/server-side files.

### PDF Viewer
View PDF documents inside a tile (e.g. rendered output or uploaded PDFs).

### Document Editor
An inline Markdown/document editor with the same UX as the editor modal but as a regular,
movable tile — create and edit documents that live in the platform's text store.

### Define Function
An embedded website tile (loads an external company site). A convenience window for keeping
a reference site beside your work.

## Operations & observability

### Workflow Runs
The **cross-workflow run monitor** — every run across **all** workflows in one
Fabric-Monitor-style list, with status and drill-down. This is the global counterpart to
the per-workflow monitoring inside the Workflows tab ([workflows.md](workflows.md)).

### KeyVault
A native, **encrypted secret store**. Add secrets (Fernet-encrypted at rest) or bulk-import
a `.env` file; the plaintext is never shown again — the tile displays metadata and an
obfuscated value only. Reference a secret from a workflow `http_request` as
`${secret.NAME}`: the value is injected server-side at the request boundary and masked in
the trace, so the model never sees it.

### Audit Dashboard
A browsable view of **audit trace events** — what the agent and tools did: tool calls,
runs, outcomes. Use it to see *exactly* what happened on a turn or a workflow run.

### System Logs
A Splunk/Datadog-style, read-only **log explorer** for backend events — search and filter
platform logs without leaving the app.

### System Cognition
An observability console for the agent's **long-term cognitive state**: the **memories**
and **beliefs** it has recorded, and **curiosity** jobs. Pairs with the *Memory & learning*
and *Memory lookup* routing slots — turn those on to populate it. See
[ai-models.md](ai-models.md).

### Productivity
A dashboard of activity/productivity metrics surfaced from platform data.

## Administration

### Mail Settings
*(Admin.)* Manage **SMTP** configurations the backend uses to send mail. The front-end
never sends mail itself — it only configures the server side.

### Notification Recipients
Manage the **email recipients** the backend uses for automatic notification mails (e.g.
from workflow `notification` operations). Pairs with the Workflows notification operation
([workflows.md](workflows.md)).

### Application Settings
*(Admin.)* A CRUD console for platform **application settings** (key/value configuration):
search, inline-edit, create, delete. The central place for tunable platform config exposed
to admins.

### PDF Templates
Manage **PDF (LaTeX) templates** used to render documents to PDF (the builtin `pdf__render`
tool and PDF export use these). Grid of templates with a detail/edit view.

### Shell Console
A console for **guarded shell execution**. Commands are backend-guarded and require
confirmation before they run — the same safety that applies when the agent uses
`system__shell_exec` ([tools.md](tools.md)). Use for diagnostics and runtime automation.
