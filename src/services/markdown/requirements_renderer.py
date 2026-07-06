# services/markdown/requirements_renderer.py
from __future__ import annotations
from typing import Any, Dict, List, Optional

from component.logging import get_logger
from services.voice.voice_utilities import fmt_timerange

log = get_logger(__name__)

def requirements_state_to_markdown(state: Dict[str, Any], *, transcript: Optional[str] = None) -> str:
    log.infox(
        "Requirements markdown renderen gestart",
        state_keys=list(state.keys()) if isinstance(state, dict) else None,
        has_transcript=transcript is not None,
        transcript_length=len(transcript or ""),
    )
    lines: List[str] = []

    ctx = state.get("context") or {}
    title = (ctx.get("title") or "").strip()
    goal = (ctx.get("goal") or "").strip()
    scope_in = ctx.get("scope_in") or []
    scope_out = ctx.get("scope_out") or []

    log.debugx(
        "Requirements context gelezen",
        has_context=bool(ctx),
        title=title,
        has_goal=bool(goal),
        scope_in_count=len(scope_in) if isinstance(scope_in, list) else None,
        scope_out_count=len(scope_out) if isinstance(scope_out, list) else None,
    )

    if title:
        log.debugx(
            "Requirements titel toevoegen",
            title=title,
        )
        lines.append(f"# {title}")
        lines.append("")

    if goal:
        log.debugx(
            "Requirements goal toevoegen",
            goal_length=len(goal),
        )
        lines.append("## 🎯 Goal")
        lines.append(goal)
        lines.append("")

    if scope_in or scope_out:
        log.debugx(
            "Requirements scope toevoegen",
            scope_in_count=len(scope_in) if isinstance(scope_in, list) else None,
            scope_out_count=len(scope_out) if isinstance(scope_out, list) else None,
        )
        lines.append("## 📦 Scope")
        if scope_in:
            lines.append("**In scope:**")
            for s in scope_in:
                if isinstance(s, str) and s.strip():
                    log.debugx(
                        "In-scope item toevoegen",
                        item=s.strip(),
                    )
                    lines.append(f"- {s.strip()}")
        if scope_out:
            lines.append("")
            lines.append("**Out of scope:**")
            for s in scope_out:
                if isinstance(s, str) and s.strip():
                    log.debugx(
                        "Out-of-scope item toevoegen",
                        item=s.strip(),
                    )
                    lines.append(f"- {s.strip()}")
        lines.append("")

    stakeholders = state.get("stakeholders") or []
    if stakeholders:
        log.debugx(
            "Stakeholders sectie toevoegen",
            stakeholder_count=len(stakeholders) if isinstance(stakeholders, list) else None,
        )
        lines.append("## 🧑‍🤝‍🧑 Stakeholders")
        for sh in stakeholders:
            if not isinstance(sh, dict):
                log.debugx(
                    "Stakeholder overgeslagen: geen dict",
                    stakeholder_type=type(sh).__name__,
                )
                continue
            name = (sh.get("name") or "").strip()
            role = (sh.get("role") or "").strip()
            needs = sh.get("needs") or []
            if not name:
                log.debugx(
                    "Stakeholder overgeslagen: naam ontbreekt",
                    stakeholder_keys=list(sh.keys()),
                )
                continue
            log.debugx(
                "Stakeholder toevoegen",
                name=name,
                role=role,
                needs_count=len(needs) if isinstance(needs, list) else None,
            )
            lines.append(f"- **{name}**" + (f" — _{role}_" if role else ""))
            for n in needs:
                if isinstance(n, str) and n.strip():
                    lines.append(f"  - {n.strip()}")
        lines.append("")

    glossary = state.get("glossary") or []
    if glossary:
        log.debugx(
            "Glossary sectie toevoegen",
            glossary_count=len(glossary) if isinstance(glossary, list) else None,
        )
        lines.append("## 📚 Glossary")
        for g in glossary:
            if not isinstance(g, dict):
                log.debugx(
                    "Glossary item overgeslagen: geen dict",
                    glossary_item_type=type(g).__name__,
                )
                continue
            term = (g.get("term") or "").strip()
            definition = (g.get("definition") or "").strip()
            if term and definition:
                log.debugx(
                    "Glossary item toevoegen",
                    term=term,
                    definition_length=len(definition),
                )
                lines.append(f"- **{term}**: {definition}")
        lines.append("")

    us = state.get("user_stories") or []
    if us:
        log.debugx(
            "User stories sectie toevoegen",
            user_story_count=len(us) if isinstance(us, list) else None,
        )
        lines.append("## 🧩 User Stories")
        for it in us:
            if not isinstance(it, dict):
                log.debugx(
                    "User story overgeslagen: geen dict",
                    user_story_type=type(it).__name__,
                )
                continue
            _id = (it.get("id") or "").strip()
            story = (it.get("story") or "").strip()
            pr = (it.get("priority") or "").strip()
            st = (it.get("status") or "").strip()
            ac = it.get("acceptance_criteria") or []
            tr = fmt_timerange(it.get("start_s"), it.get("end_s"))
            ev = (it.get("evidence") or "").strip()
            if not story:
                log.debugx(
                    "User story overgeslagen: story ontbreekt",
                    user_story_keys=list(it.keys()),
                )
                continue
            log.debugx(
                "User story toevoegen",
                id=_id,
                priority=pr,
                status=st,
                timerange=tr,
                has_evidence=bool(ev),
                acceptance_criteria_count=len(ac) if isinstance(ac, list) else None,
            )
            head = " · ".join([x for x in [_id, pr, st, tr] if x]).strip()
            lines.append(f"- **{story}**" + (f"  \n  _{head}_" if head else ""))
            if ac:
                lines.append("  \n  _Acceptance criteria:_")
                for c in ac:
                    if isinstance(c, str) and c.strip():
                        lines.append(f"  - {c.strip()}")
            if ev:
                lines.append(f"  \n  > “{ev}”")
        lines.append("")

    fr = state.get("functional_requirements") or []
    if fr:
        log.debugx(
            "Functional requirements sectie toevoegen",
            functional_requirement_count=len(fr) if isinstance(fr, list) else None,
        )
        lines.append("## ✅ Functional Requirements")
        for it in fr:
            if not isinstance(it, dict):
                log.debugx(
                    "Functional requirement overgeslagen: geen dict",
                    requirement_type=type(it).__name__,
                )
                continue
            _id = (it.get("id") or "").strip()
            req = (it.get("requirement") or "").strip()
            rat = (it.get("rationale") or "").strip()
            pr = (it.get("priority") or "").strip()
            st = (it.get("status") or "").strip()
            deps = it.get("dependencies") or []
            ac = it.get("acceptance_criteria") or []
            tr = fmt_timerange(it.get("start_s"), it.get("end_s"))
            ev = (it.get("evidence") or "").strip()
            if not req:
                log.debugx(
                    "Functional requirement overgeslagen: requirement ontbreekt",
                    requirement_keys=list(it.keys()),
                )
                continue

            log.debugx(
                "Functional requirement toevoegen",
                id=_id,
                priority=pr,
                status=st,
                timerange=tr,
                has_rationale=bool(rat),
                has_evidence=bool(ev),
                dependency_count=len(deps) if isinstance(deps, list) else None,
                acceptance_criteria_count=len(ac) if isinstance(ac, list) else None,
            )

            meta = " · ".join([x for x in [_id, pr, st, tr] if x]).strip()
            lines.append(f"- **{req}**" + (f"  \n  _{meta}_" if meta else ""))
            if rat:
                lines.append(f"  \n  _Rationale:_ {rat}")
            if deps:
                dep_list = [d.strip() for d in deps if isinstance(d, str) and d.strip()]
                if dep_list:
                    log.debugx(
                        "Functional requirement dependencies toevoegen",
                        id=_id,
                        dependency_count=len(dep_list),
                    )
                    lines.append(f"  \n  _Dependencies:_ " + ", ".join(dep_list))
            if ac:
                lines.append("  \n  _Acceptance criteria:_")
                for c in ac:
                    if isinstance(c, str) and c.strip():
                        lines.append(f"  - {c.strip()}")
            if ev:
                lines.append(f"  \n  > “{ev}”")
        lines.append("")

    nfr = state.get("nonfunctional_requirements") or []
    if nfr:
        log.debugx(
            "Non-functional requirements sectie toevoegen",
            nonfunctional_requirement_count=len(nfr) if isinstance(nfr, list) else None,
        )
        lines.append("## 🛡️ Non-Functional Requirements")
        for it in nfr:
            if not isinstance(it, dict):
                log.debugx(
                    "Non-functional requirement overgeslagen: geen dict",
                    requirement_type=type(it).__name__,
                )
                continue
            _id = (it.get("id") or "").strip()
            cat = (it.get("category") or "").strip()
            req = (it.get("requirement") or "").strip()
            metric = (it.get("metric") or "").strip()
            pr = (it.get("priority") or "").strip()
            st = (it.get("status") or "").strip()
            tr = fmt_timerange(it.get("start_s"), it.get("end_s"))
            ev = (it.get("evidence") or "").strip()
            if not req:
                log.debugx(
                    "Non-functional requirement overgeslagen: requirement ontbreekt",
                    requirement_keys=list(it.keys()),
                )
                continue
            log.debugx(
                "Non-functional requirement toevoegen",
                id=_id,
                category=cat,
                priority=pr,
                status=st,
                timerange=tr,
                has_metric=bool(metric),
                has_evidence=bool(ev),
            )
            meta = " · ".join([x for x in [_id, cat, pr, st, tr] if x]).strip()
            lines.append(f"- **{req}**" + (f"  \n  _{meta}_" if meta else ""))
            if metric:
                lines.append(f"  \n  _Metric:_ {metric}")
            if ev:
                lines.append(f"  \n  > “{ev}”")
        lines.append("")

    def _simple_list(title: str, items: Any) -> None:
        nonlocal lines
        log.debugx(
            "Eenvoudige requirements lijst verwerken gestart",
            title=title,
            raw_type=type(items).__name__,
            raw_count=len(items) if isinstance(items, list) else None,
        )
        items = items or []
        items = [x.strip() for x in items if isinstance(x, str) and x.strip()]
        if not items:
            log.debugx(
                "Eenvoudige requirements lijst overgeslagen: geen items",
                title=title,
            )
            return
        lines.append(title)
        for x in items:
            log.debugx(
                "Eenvoudig lijstitem toevoegen",
                title=title,
                item=x,
            )
            lines.append(f"- {x}")
        lines.append("")
        log.debugx(
            "Eenvoudige requirements lijst verwerken afgerond",
            title=title,
            item_count=len(items),
        )

    _simple_list("## 🧠 Assumptions", state.get("assumptions"))
    _simple_list("## ⛓️ Constraints", state.get("constraints"))

    risks = state.get("risks") or []
    if risks:
        log.debugx(
            "Risks sectie toevoegen",
            risk_count=len(risks) if isinstance(risks, list) else None,
        )
        lines.append("## ⚠️ Risks")
        for r in risks:
            if not isinstance(r, dict):
                log.debugx(
                    "Risk overgeslagen: geen dict",
                    risk_type=type(r).__name__,
                )
                continue
            rr = (r.get("risk") or "").strip()
            impact = (r.get("impact") or "").strip()
            mit = (r.get("mitigation") or "").strip()
            if not rr:
                log.debugx(
                    "Risk overgeslagen: risk ontbreekt",
                    risk_keys=list(r.keys()),
                )
                continue
            log.debugx(
                "Risk toevoegen",
                risk=rr,
                has_impact=bool(impact),
                has_mitigation=bool(mit),
            )
            lines.append(f"- **{rr}**")
            if impact:
                lines.append(f"  \n  _Impact:_ {impact}")
            if mit:
                lines.append(f"  \n  _Mitigation:_ {mit}")
        lines.append("")

    decisions = state.get("decisions") or []
    if decisions:
        log.debugx(
            "Decisions sectie toevoegen",
            decision_count=len(decisions) if isinstance(decisions, list) else None,
        )
        lines.append("## 🧠 Decisions")
        for d in decisions:
            if not isinstance(d, dict):
                log.debugx(
                    "Decision overgeslagen: geen dict",
                    decision_type=type(d).__name__,
                )
                continue
            dec = (d.get("decision") or "").strip()
            rat = (d.get("rationale") or "").strip()
            ev = (d.get("evidence") or "").strip()
            tr = fmt_timerange(d.get("start_s"), d.get("end_s"))
            if not dec:
                log.debugx(
                    "Decision overgeslagen: decision ontbreekt",
                    decision_keys=list(d.keys()),
                )
                continue
            log.debugx(
                "Decision toevoegen",
                decision=dec,
                timerange=tr,
                has_rationale=bool(rat),
                has_evidence=bool(ev),
            )
            lines.append(f"- **{dec}**" + (f"  \n  _{tr}_" if tr else ""))
            if rat:
                lines.append(f"  \n  _Rationale:_ {rat}")
            if ev:
                lines.append(f"  \n  > “{ev}”")
        lines.append("")

    _simple_list("## ❓ Open Questions", state.get("open_questions"))
    _simple_list("## 📝 Notes", state.get("notes"))

    result = "\n".join(lines).strip() + "\n"
    log.infox(
        "Requirements markdown renderen afgerond",
        line_count=len(lines),
        result_length=len(result),
        has_output=bool(result.strip()),
    )
    return result