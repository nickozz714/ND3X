# Slash Commands

Type **`/`** at the start of a chat message to open an autocomplete popup of
commands — a fast way to steer a turn without clicking around.

## Using them
- Start typing `/…`; use **↑ ↓** to move, **Tab/Enter** to complete, **Esc** to close.
- For commands that take a **name** (`/skill`, `/model`, `/tool`, `/workflow`), start
  typing the argument and a second list suggests matching names to pick.

## Built-in commands
| Command | What it does |
|---------|--------------|
| `/plan <question>` | Ask for a plan first (the agent proposes steps before acting). |
| `/guide <question>` | Extra step-by-step guidance for this message (helps small/local models). |
| `/goal <goal>` | Goal mode: keep working (higher budgets) until the goal is provably met. `/goal stop` ends it. |
| `/model <name\|auto>` | Switch the chat model for this thread (`auto` = the routing slot). |
| `/light <q>` / `/full <q>` | Force the compact (light) or full prompt for this message. |
| `/skill <name> <q>` | Pre-select a skill for this message. |
| `/tool <name> <q>` | Steer the agent to a specific tool. |
| `/workflow <name> [input]` | Start a workflow by name. |
| `/web <q>` | Research this with web search. |
| `/img <prompt>` | Generate an image (needs the image-generation slot). |
| `/task <instruction>` | Start a background task (subagent) and keep working. |
| `/new [question]` | Start a new thread. |
| `/compact` | Compress the thread history (summary + fresh context). |
| `/help` | List all available commands. |

## Custom commands
Under **AI Workbench → Slash Commands** you can create your own commands
(Expert-managed). A custom command is a **prompt template**; `{args}` is replaced by
whatever you type after the command, and any `@Token` prompt-variables inside keep
working. Example: a `/summary` command whose template says *"Summarise the following
in five bullets: {args}"*. Your commands appear in the same autocomplete as the
built-ins.
