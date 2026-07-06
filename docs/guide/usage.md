# Usage

The **Usage** tab is the cost & token-consumption dashboard. It answers three questions:
*how full is the current conversation's context?*, *am I within my monthly budget?*, and
*where are my tokens going?*

Open **AI Workbench → Usage**.

## 1. This conversation (context window)

For the active chat thread it shows:

- **Context used / context window** with a percentage and **tokens left** — how close the
  conversation is to filling the active model's context window.
- **Total this conversation** — tokens used, split **in / out**.
- A **Compact** action — summarises the conversation and **resets the context window** for
  the next turn while **preserving the full thread**. Use this when a long conversation is
  near its context limit and answers start to degrade.

If the active model has no registered context window, it shows raw tokens used instead of a
percentage.

## 2. Monthly budget (burn-down)

Set an optional **monthly token budget** (tokens/month; empty = no cap). The dashboard then
shows **used / budget** and **tokens left this month** as a burn-down. A cost budget
(USD/month) can also be tracked. Use this as a guardrail against runaway spend — pair it
with cost-optimized routing in [ai-models.md](ai-models.md).

## 3. Totals & breakdowns

Aggregate usage, broken down three ways:

- **By model** — which models consumed the most tokens.
- **By provider** — cloud vs local, OpenAI vs Anthropic, etc.
- **By stage** — which part of a turn (skill selection, execution/answer, cognition,
  memory, …) is spending tokens.

There's also a **per-thread** breakdown listing recent conversations with their token
totals and a per-stage split.

## How to use it

- **Diagnosing cost:** read **by stage** — if "selection" or "cognition" is heavy, move
  those slots to cheaper/local models ([ai-models.md](ai-models.md)); if "execution" is
  heavy, your tasks are genuinely large (consider compaction or tighter skills).
- **Diagnosing slow/poor answers in a long chat:** check the **context window %** — if it's
  near full, **Compact**.
- **Controlling spend:** set a **monthly budget** and check the burn-down; verify changes
  by watching **by stage** before/after a routing change.
- **Attributing cost:** use **by model / by provider** to see what your provider bill maps
  to.
