"""Starter templates for meeting profiles — guidance the user can create from.

These mirror the built-in code profiles (general meeting, requirements) and add
common meeting types. The FE offers them in "Start from template" to prefill the
New Profile form; the user reviews/edits and saves a real (DB) profile.

Each template is intentionally extensive: the ``instructions`` describe what the
meeting assistant should listen for and how to phrase the result, and the
``output_template`` is a full markdown skeleton (headings + guiding bullets) the
model fills in. Users are expected to trim these down to taste.
"""
from __future__ import annotations

from typing import Any, Dict, List

MEETING_PROFILE_TEMPLATES: List[Dict[str, Any]] = [
    {
        "key": "general_meeting",
        "name": "General meeting",
        "description": "Executive summary, decisions, action items and open questions — the default meeting flow.",
        "instructions": (
            "Produce concise, professional minutes of a general business meeting.\n"
            "- Open with a 3–5 sentence executive summary that a non-attendee could read in 20 seconds.\n"
            "- Record every decision that was actually agreed (not merely discussed); phrase each as a single declarative sentence.\n"
            "- Capture action items as `owner — task — due date`; if an owner or date was not stated, write `(unassigned)` / `(no date)` rather than guessing.\n"
            "- Note discussion highlights and the key arguments for/against major points, but stay neutral and do not editorialise.\n"
            "- Collect anything left unresolved under open questions so nothing is silently dropped.\n"
            "- Attribute statements to speakers only when the transcript makes the speaker clear."
        ),
        "language": "",
        "output_template": (
            "## Executive summary\n"
            "_2–5 sentences capturing purpose, outcome and any decision that matters._\n\n"
            "## Decisions\n"
            "- \n\n"
            "## Action items\n"
            "| Owner | Action | Due |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |\n\n"
            "## Discussion highlights\n"
            "- \n\n"
            "## Open questions\n"
            "- "
        ),
    },
    {
        "key": "requirements",
        "name": "Requirements engineering",
        "description": "Structured elicitation of functional & non-functional requirements, constraints, assumptions and risks.",
        "instructions": (
            "Act as a requirements engineer turning the conversation into a structured, review-ready requirements set.\n"
            "- Extract functional requirements as atomic, testable statements; prefer the form 'The system shall …'. Give each a short stable ID (FR-1, FR-2, …).\n"
            "- Extract non-functional requirements (performance, security, availability, usability, compliance, scalability, maintainability) as NFR-n and quantify them whenever a number was mentioned.\n"
            "- Separate genuine constraints (technical, regulatory, budget, timeline, organisational) from assumptions that still need validation.\n"
            "- Record the stakeholder / source behind each requirement when the transcript makes it clear, plus a priority (MoSCoW: Must / Should / Could / Won't) if stated.\n"
            "- Flag conflicts, ambiguities and anything that needs follow-up as open questions or risks — do not invent requirements that were not discussed.\n"
            "- Note acceptance criteria or 'definition of done' hints where they were voiced."
        ),
        "language": "",
        "output_template": (
            "## Context & scope\n"
            "_What is being built and the boundaries that were agreed._\n\n"
            "## Functional requirements\n"
            "| ID | Requirement (shall…) | Source | Priority |\n"
            "| --- | --- | --- | --- |\n"
            "| FR-1 |  |  |  |\n\n"
            "## Non-functional requirements\n"
            "| ID | Category | Requirement / target | Priority |\n"
            "| --- | --- | --- | --- |\n"
            "| NFR-1 |  |  |  |\n\n"
            "## Constraints\n"
            "- \n\n"
            "## Assumptions (to validate)\n"
            "- \n\n"
            "## Acceptance criteria\n"
            "- \n\n"
            "## Risks & open questions\n"
            "- "
        ),
    },
    {
        "key": "standup",
        "name": "Daily standup",
        "description": "Per-person done / today / blockers, with blockers surfaced and owned.",
        "instructions": (
            "Summarise a short daily standup. Keep it tight — this is a status check, not minutes.\n"
            "- For each participant, capture: what they completed since last standup, what they intend to do today, and any blockers.\n"
            "- Pull every blocker into its own prominent list with a suggested owner / who can unblock it.\n"
            "- Note anything that slipped or changed scope, and any dependency between people.\n"
            "- Do not pad: omit a section for a person if they only gave a status with no blockers."
        ),
        "language": "",
        "output_template": (
            "## Standup — {date}\n\n"
            "### Per person\n"
            "**{name}**\n"
            "- Done: \n"
            "- Today: \n"
            "- Blockers: \n\n"
            "### Blockers (with owners)\n"
            "| Blocker | Affects | Owner / unblocker |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |\n\n"
            "### Dependencies & slippage\n"
            "- "
        ),
    },
    {
        "key": "one_on_one",
        "name": "1:1",
        "description": "Topics, two-way feedback, growth and agreed actions — kept factual and confidential.",
        "instructions": (
            "Capture a manager/report 1:1 in a respectful, confidential tone.\n"
            "- List the topics discussed in the order they came up.\n"
            "- Record feedback in both directions (manager → report and report → manager) and keep it specific and behavioural.\n"
            "- Capture career, growth and development notes, plus wellbeing / workload signals if raised.\n"
            "- End with concrete agreed actions and who owns each.\n"
            "- Stay factual and neutral; never speculate about performance or sensitive personal matters beyond what was actually said."
        ),
        "language": "",
        "output_template": (
            "## Topics\n"
            "- \n\n"
            "## Feedback\n"
            "- To report: \n"
            "- To manager: \n\n"
            "## Growth & development\n"
            "- \n\n"
            "## Wellbeing / workload\n"
            "- \n\n"
            "## Agreed actions\n"
            "| Owner | Action | Due |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |"
        ),
    },
    {
        "key": "sales_call",
        "name": "Sales / discovery call",
        "description": "Needs, pains, objections, budget, decision process, competitors and next steps.",
        "instructions": (
            "Capture a sales or discovery call from the seller's perspective, ready to drop into a CRM.\n"
            "- Record the customer's stated needs and underlying pains (the 'why now').\n"
            "- List objections, risks and concerns raised, and how (or whether) they were addressed.\n"
            "- Capture budget / pricing signals, timeline and any procurement or legal hurdles.\n"
            "- Map the decision process: decision makers, influencers, champions, and the buying criteria.\n"
            "- Note competitors or incumbent solutions mentioned, and any differentiators that landed.\n"
            "- End with concrete next steps, each with an owner and a date, and a short deal-health read (signals for/against)."
        ),
        "language": "",
        "output_template": (
            "## Account & contacts\n"
            "- \n\n"
            "## Needs & pains\n"
            "- \n\n"
            "## Objections / concerns\n"
            "| Objection | Response | Resolved? |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |\n\n"
            "## Budget, timeline & procurement\n"
            "- \n\n"
            "## Decision process\n"
            "- Decision makers: \n"
            "- Champion: \n"
            "- Buying criteria: \n\n"
            "## Competition\n"
            "- \n\n"
            "## Next steps\n"
            "| Owner | Step | Due |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |\n\n"
            "## Deal health\n"
            "_Signals for / against, and confidence._"
        ),
        # Meeting-driven actions (#9): live read-only look-ups, suggest-first.
        "action_policy": {
            "enabled": True,
            "autonomy": "suggest",
            "allowed_actions": ["lookup", "answer"],
            "allowed_tools": ["web_search"],
            "triggers": ["competitor", "pricing", "integration", "company", "product", "feature"],
            "min_confidence": 0.6,
            "max_per_tick": 2,
            "action_budget": 24,
        },
    },
    {
        "key": "interview",
        "name": "Interview",
        "description": "Per-question answers, evidence-based strengths & concerns, and a hire recommendation.",
        "instructions": (
            "Summarise a candidate interview objectively and job-relevantly.\n"
            "- Capture the candidate's answer per question or topic, briefly and faithfully.\n"
            "- Note strengths and concerns, each backed by a concrete example or quote from the conversation.\n"
            "- Map signals to the role's competencies where they are clear.\n"
            "- Conclude with a hire / no-hire / lean and a short rationale, plus suggested follow-up areas for the next round.\n"
            "- Avoid bias: stick to job-relevant evidence and do not infer protected characteristics or speculate beyond what was said."
        ),
        "language": "",
        "output_template": (
            "## Candidate & role\n"
            "- \n\n"
            "## Answers (per question/topic)\n"
            "**{question}**\n"
            "- \n\n"
            "## Strengths (with evidence)\n"
            "- \n\n"
            "## Concerns (with evidence)\n"
            "- \n\n"
            "## Competency signals\n"
            "| Competency | Signal | Rating |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |\n\n"
            "## Recommendation\n"
            "_Hire / No-hire / Lean + rationale + follow-ups for next round._"
        ),
    },
    {
        "key": "retrospective",
        "name": "Retrospective",
        "description": "What went well / what didn't / improvement actions — blameless and themed.",
        "instructions": (
            "Capture a blameless team retrospective.\n"
            "- Group what went well, what didn't, and ideas/experiments to try, clustering recurring themes.\n"
            "- Turn the most important learnings into concrete improvement actions with an owner and a target.\n"
            "- Note any action items carried over from a previous retro and whether they were done.\n"
            "- Keep language neutral and focused on systems and process, never on individuals."
        ),
        "language": "",
        "output_template": (
            "## What went well\n"
            "- \n\n"
            "## What didn't go well\n"
            "- \n\n"
            "## Ideas / experiments to try\n"
            "- \n\n"
            "## Improvement actions\n"
            "| Owner | Action | Target |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |\n\n"
            "## Carried-over actions\n"
            "- "
        ),
    },
    {
        "key": "project_kickoff",
        "name": "Project kickoff",
        "description": "Goals, scope, stakeholders, milestones, risks and ways of working.",
        "instructions": (
            "Capture a project kickoff so the team leaves aligned.\n"
            "- State the project goal and the business outcome it serves, plus the success metrics named.\n"
            "- Record what is in scope and explicitly out of scope.\n"
            "- List stakeholders and roles (sponsor, lead, delivery, RACI hints where given).\n"
            "- Capture milestones / timeline, key dependencies, and the agreed ways of working (cadence, tools, comms).\n"
            "- Surface risks and assumptions, and the first concrete next steps with owners."
        ),
        "language": "",
        "output_template": (
            "## Goal & success metrics\n"
            "- \n\n"
            "## Scope\n"
            "- In scope: \n"
            "- Out of scope: \n\n"
            "## Stakeholders & roles\n"
            "| Name | Role | Responsibility |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |\n\n"
            "## Milestones & timeline\n"
            "- \n\n"
            "## Ways of working\n"
            "- Cadence: \n"
            "- Tools / comms: \n\n"
            "## Risks & assumptions\n"
            "- \n\n"
            "## Next steps\n"
            "| Owner | Step | Due |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |"
        ),
    },
    {
        "key": "client_intake",
        "name": "Client intake / discovery",
        "description": "Client context, goals, current situation, requirements and proposed next steps.",
        "instructions": (
            "Capture a client intake / discovery session that a consultant could act on.\n"
            "- Record the client's context, current situation and the problem they want solved.\n"
            "- Capture their goals and what success looks like to them, plus constraints (budget, timeline, tech, compliance).\n"
            "- Note key requirements and 'must-haves' vs 'nice-to-haves'.\n"
            "- Identify stakeholders, decision process and any existing systems/vendors.\n"
            "- End with proposed next steps and what you owe them (proposal, demo, estimate)."
        ),
        "language": "",
        "output_template": (
            "## Client & context\n"
            "- \n\n"
            "## Problem & current situation\n"
            "- \n\n"
            "## Goals & definition of success\n"
            "- \n\n"
            "## Requirements (must-have / nice-to-have)\n"
            "- Must: \n"
            "- Nice: \n\n"
            "## Constraints\n"
            "- \n\n"
            "## Stakeholders & decision process\n"
            "- \n\n"
            "## Next steps & deliverables\n"
            "| Owner | Step | Due |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |"
        ),
    },
    {
        "key": "workshop",
        "name": "Workshop / brainstorm",
        "description": "Themes, ideas clustered, top picks and follow-up experiments.",
        "instructions": (
            "Capture a workshop or brainstorming session without losing ideas.\n"
            "- Record the framing question / objective of the session.\n"
            "- Capture all ideas raised, then cluster them into themes; keep the original phrasing where it adds colour.\n"
            "- Highlight the ideas that gained the most support or were selected to pursue, and why.\n"
            "- Capture decisions, parked ideas, and concrete follow-up experiments / actions with owners."
        ),
        "language": "",
        "output_template": (
            "## Objective\n"
            "- \n\n"
            "## Ideas by theme\n"
            "**{theme}**\n"
            "- \n\n"
            "## Selected to pursue\n"
            "- \n\n"
            "## Parked / out of scope\n"
            "- \n\n"
            "## Follow-up experiments\n"
            "| Owner | Experiment | By when |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |"
        ),
    },
    {
        "key": "incident_postmortem",
        "name": "Incident postmortem",
        "description": "Blameless timeline, impact, root cause, and corrective actions.",
        "instructions": (
            "Capture a blameless incident postmortem.\n"
            "- Summarise the incident: what happened, severity, and customer/business impact (with numbers if stated).\n"
            "- Reconstruct a timeline (detection → diagnosis → mitigation → resolution) with timestamps where mentioned.\n"
            "- Identify the root cause(s) and contributing factors; separate the trigger from the underlying cause.\n"
            "- Note what went well and what made the response harder.\n"
            "- List corrective / preventive actions as owned, dated follow-ups. Focus on systems, never on blaming individuals."
        ),
        "language": "",
        "output_template": (
            "## Summary & impact\n"
            "- Severity: \n"
            "- Impact: \n\n"
            "## Timeline\n"
            "| Time | Event |\n"
            "| --- | --- |\n"
            "|  |  |\n\n"
            "## Root cause & contributing factors\n"
            "- Trigger: \n"
            "- Root cause: \n"
            "- Contributing factors: \n\n"
            "## What went well / what hurt\n"
            "- \n\n"
            "## Corrective actions\n"
            "| Owner | Action | Due |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |"
        ),
    },
    {
        "key": "steering_committee",
        "name": "Steering committee / board",
        "description": "Status against plan, key decisions, risks/issues and approvals sought.",
        "instructions": (
            "Capture a steering committee or board meeting at an executive altitude.\n"
            "- Lead with status against plan (on track / at risk / off track) per workstream, with the headline reason.\n"
            "- Record decisions taken and approvals granted or sought (budget, scope, go/no-go).\n"
            "- Surface the top risks and issues with severity and the mitigation/owner.\n"
            "- Capture escalations requiring sponsor action.\n"
            "- Keep it crisp and outcome-oriented; avoid operational detail unless it drives a decision."
        ),
        "language": "",
        "output_template": (
            "## Status against plan\n"
            "| Workstream | RAG | Headline |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |\n\n"
            "## Decisions & approvals\n"
            "- \n\n"
            "## Risks & issues\n"
            "| Risk/Issue | Severity | Mitigation | Owner |\n"
            "| --- | --- | --- | --- |\n"
            "|  |  |  |  |\n\n"
            "## Escalations for sponsor\n"
            "- \n\n"
            "## Actions\n"
            "| Owner | Action | Due |\n"
            "| --- | --- | --- |\n"
            "|  |  |  |"
        ),
    },
]
