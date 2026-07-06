# Users

*(Admin only.)* The **Users** tab is account & role administration. Open
**AI Workbench → Users**.

## What you can do

- See every user (email, display name, current roles).
- **Toggle roles** per user and save.

Role management is the platform's authorization model — there is no separate permission
matrix; **what a user can see and do is determined entirely by their roles.**

## The three roles

| Role | Grants |
|------|--------|
| **User** | Day-to-day use: chat with the agent, run workflows, read most tiles. |
| **Expert** | Everything a User can do **plus** managing Skills, Tools, MCP Servers, and system-level skills. The builder of capabilities. |
| **Admin** | Everything, **including** the **AI Models** tab (provider keys, routing) and this **Users** tab. |

Roles are additive — an Admin typically also holds Expert and User. Assign the **lowest
role that lets someone do their job**:

- Someone who only chats and runs workflows → **User**.
- Someone who builds/maintains skills, tools, and MCP connections → **Expert**.
- Someone who manages model routing, API keys, costs, and accounts → **Admin**.

## Notes & safety

- Tabs and tiles that require a role are hidden/blocked for users without it (e.g. **AI
  Models** and **Users** are Admin-only; much of the skill/tool/MCP management is
  Expert-gated).
- Removing your **own** Admin role is a one-way action that locks you out of admin
  surfaces — the UI guards against accidentally doing so; keep at least one other Admin.
- Adding **Admin** grants full control including provider keys and spend — grant it
  sparingly.
