# Builtins & System

An admin surface for the platform's **built-in tools** and its **system / runtime
skills** — the parts that ship with ND3X rather than being configured as data.

## Built-in tools
The agent always has a set of built-in tools (read/inspect files, search, shell,
web search, run/list workflows, query data, …). Here you can **enable or disable**
individual built-ins — e.g. turn off shell execution in a locked-down deployment.
A disabled built-in simply isn't offered to the agent.

## System & runtime skills
**System skills** are code-authoritative contracts the agent always follows (the
response shape, tool-call rules, completion integrity, the handoff format).
**Runtime skills** back certain built-in capabilities. You can toggle these on/off
here, but do so with care: disabling a contract changes how the agent behaves.

## Azure login helpers
For features that use Azure (e.g. Fabric Data Agents via an Azure CLI session), this
tab also offers helpers to start/refresh an Azure login on the host.

> This tab is **admin-only**. Most deployments never need to change anything here —
> the defaults are what make a fresh install work out of the box.
