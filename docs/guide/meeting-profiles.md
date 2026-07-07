# Meeting Profiles

A **meeting profile** shapes how a recorded or live meeting is turned into notes.
Pick one when you capture audio in the **Meeting** tile; the profile decides the
language, the structure of the output, and what (if anything) to watch for live.

## What a profile defines
- **Instructions** — how to summarise (tone, focus, what to include/ignore).
- **Language** — the language of the notes.
- **Output template** — the shape of the result (e.g. *Summary → Decisions →
  Action items*, or a requirements document).
- **Action policy** — for the live lane, what kinds of items to detect while the
  meeting runs (e.g. action items, decisions, questions).

## Using them
Manage profiles under **AI Workbench → Meeting Profiles**: create, edit, and
enable/disable them, or **start from a template**. In the Meeting tile you then
choose a profile before (or while) recording. A live meeting can run a read-only
**action-detection** lane driven by the profile's action policy, surfacing items as
they come up.

Meeting capture needs a **transcription** model assigned (see
[ai-models.md](ai-models.md)); the notes are written by the agent's model.
