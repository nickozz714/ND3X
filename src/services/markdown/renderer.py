from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Literal
from component.logging import get_logger
from services.voice.voice_utilities import fmt_timerange, normalize_mermaid_mindmap

log = get_logger(__name__)

Mode = Literal["final", "live"]  # final = post-processed summary, live = streaming state

# --- You already have these helpers somewhere ---
# def _fmt_timerange(start_s: Any, end_s: Any) -> str: ...
# def normalize_mermaid_mindmap(mm: str) -> str: ...


def _s(x: Any) -> str:
    """Safe string normalize."""
    log.debugx(
        "Waarde normaliseren naar veilige string gestart",
        value_type=type(x).__name__,
        is_none=x is None,
    )
    result = (x or "").strip() if isinstance(x, str) or x is None else str(x).strip()
    log.debugx(
        "Waarde normaliseren naar veilige string afgerond",
        value_type=type(x).__name__,
        result_length=len(result),
    )
    return result


def _is_nonempty_str(x: Any) -> bool:
    log.debugx(
        "Controleren of waarde een niet-lege string is gestart",
        value_type=type(x).__name__,
    )
    result = isinstance(x, str) and bool(x.strip())
    log.debugx(
        "Controleren of waarde een niet-lege string is afgerond",
        value_type=type(x).__name__,
        is_nonempty_str=result,
    )
    return result


@dataclass(frozen=True)
class RenderContext:
    mode: Mode
    transcript: Optional[str] = None

    def __post_init__(self):
        log.debugx(
            "RenderContext aangemaakt",
            mode=self.mode,
            has_transcript=self.transcript is not None,
            transcript_length=len(self.transcript or ""),
        )


SectionFn = Callable[[Dict[str, Any], RenderContext], List[str]]


class MarkdownRenderer:
    """
    Extensible markdown builder: register sections (plugins) and render in order.
    """

    def __init__(self, sections: Optional[Sequence[SectionFn]] = None) -> None:
        log.debugx(
            "MarkdownRenderer initialiseren",
            has_sections=sections is not None,
            section_count=len(sections) if sections else 0,
        )
        self._sections: List[SectionFn] = list(sections) if sections else []
        log.debugx(
            "MarkdownRenderer geïnitialiseerd",
            section_count=len(self._sections),
        )

    def register(self, section: SectionFn) -> "MarkdownRenderer":
        log.debugx(
            "Markdown sectie registreren gestart",
            section_name=getattr(section, "__name__", None),
            current_section_count=len(self._sections),
        )
        self._sections.append(section)
        log.debugx(
            "Markdown sectie registreren afgerond",
            section_name=getattr(section, "__name__", None),
            section_count=len(self._sections),
        )
        return self

    def extend(self, sections: Sequence[SectionFn]) -> "MarkdownRenderer":
        log.debugx(
            "Markdown secties uitbreiden gestart",
            added_section_count=len(sections),
            current_section_count=len(self._sections),
            section_names=[getattr(s, "__name__", None) for s in sections],
        )
        self._sections.extend(sections)
        log.debugx(
            "Markdown secties uitbreiden afgerond",
            section_count=len(self._sections),
        )
        return self

    def render(self, data: Dict[str, Any], *, mode: Mode = "final", transcript: Optional[str] = None) -> str:
        log.infox(
            "Markdown renderen gestart",
            mode=mode,
            data_keys=list(data.keys()) if isinstance(data, dict) else None,
            section_count=len(self._sections),
            has_transcript=transcript is not None,
            transcript_length=len(transcript or ""),
        )
        ctx = RenderContext(mode=mode, transcript=transcript)
        blocks: List[str] = []

        for sec in self._sections:
            log.debugx(
                "Markdown sectie renderen gestart",
                section_name=getattr(sec, "__name__", None),
                mode=mode,
            )
            out = sec(data, ctx)
            log.debugx(
                "Markdown sectie renderen afgerond",
                section_name=getattr(sec, "__name__", None),
                mode=mode,
                line_count=len(out) if out else 0,
                has_output=bool(out),
            )
            if out:
                # each section returns "lines"; join that section with \n, and sections with \n\n
                blocks.append("\n".join(out).rstrip())

        text = "\n\n".join([b for b in blocks if b.strip()]).strip()
        log.debugx(
            "Markdown blokken samengevoegd",
            mode=mode,
            block_count=len(blocks),
            text_length=len(text),
        )

        if not text:
            log.warningx(
                "Markdown renderen gaf lege output, fallback tekst wordt gebruikt",
                mode=mode,
            )
            if mode == "live":
                return "## 🧾 Live Notes\n_Waiting for speech…_\n"
            return "I processed the recording, but didn’t find anything to summarize."

        result = text + ("\n" if mode == "live" and not text.endswith("\n") else "")
        log.infox(
            "Markdown renderen afgerond",
            mode=mode,
            result_length=len(result),
        )
        return result


# -----------------------
# Sections (plugins)
# -----------------------

def section_summary(data: Dict[str, Any], ctx: RenderContext) -> List[str]:
    log.debugx(
        "Markdown section_summary gestart",
        mode=ctx.mode,
        data_keys=list(data.keys()) if isinstance(data, dict) else None,
    )
    v = data.get("views") or {}
    exec_s = _s(v.get("exec"))
    detailed = _s(v.get("detailed"))
    summary = _s(data.get("summary"))

    lines: List[str] = []
    if ctx.mode == "live":
        lines.append("## 🧾 Live Notes")
        lines.append(exec_s or "_Waiting for speech…_")
        if detailed:
            lines.append("")
            lines.append("## 🧠 Details")
            lines.append(detailed)
        log.debugx(
            "Markdown section_summary afgerond",
            mode=ctx.mode,
            line_count=len(lines),
            has_exec=bool(exec_s),
            has_detailed=bool(detailed),
            has_summary=bool(summary),
        )
        return lines

    # final
    if exec_s or summary:
        lines.append("## 🧾 Summary")
        lines.append(exec_s or summary)
    if detailed:
        lines.append("## 🧠 Detailed")
        lines.append(detailed)
    log.debugx(
        "Markdown section_summary afgerond",
        mode=ctx.mode,
        line_count=len(lines),
        has_exec=bool(exec_s),
        has_detailed=bool(detailed),
        has_summary=bool(summary),
    )
    return lines


def section_key_points(data: Dict[str, Any], ctx: RenderContext) -> List[str]:
    log.debugx(
        "Markdown section_key_points gestart",
        mode=ctx.mode,
    )
    v = data.get("views") or {}
    bullets = v.get("bullets") or []
    bullets = [b.strip() for b in bullets if _is_nonempty_str(b)]
    if not bullets:
        log.debugx(
            "Markdown section_key_points overgeslagen: geen bullets",
            mode=ctx.mode,
        )
        return []
    lines = ["## 📌 Key points"]
    lines.extend([f"- {b}" for b in bullets])
    log.debugx(
        "Markdown section_key_points afgerond",
        mode=ctx.mode,
        bullet_count=len(bullets),
        line_count=len(lines),
    )
    return lines


def section_speakers(data: Dict[str, Any], ctx: RenderContext) -> List[str]:
    log.debugx(
        "Markdown section_speakers gestart",
        mode=ctx.mode,
    )
    # only in "final" (your live function didn't show speakers)
    if ctx.mode != "final":
        log.debugx(
            "Markdown section_speakers overgeslagen: alleen final mode",
            mode=ctx.mode,
        )
        return []
    speakers = data.get("speakers") or []
    if not isinstance(speakers, list) or not speakers:
        log.debugx(
            "Markdown section_speakers overgeslagen: geen speakers",
            mode=ctx.mode,
            speakers_type=type(speakers).__name__,
        )
        return []

    lines = ["## 🗣️ Speakers"]
    for s in speakers:
        if not isinstance(s, dict):
            log.debugx(
                "Speaker overgeslagen: geen dict",
                speaker_type=type(s).__name__,
            )
            continue
        sid = _s(s.get("id"))
        name = s.get("name")
        if not sid:
            log.debugx(
                "Speaker overgeslagen: id ontbreekt",
                speaker_keys=list(s.keys()),
            )
            continue
        lines.append(f"- **{sid}**: {name}" if name else f"- **{sid}**")
    log.debugx(
        "Markdown section_speakers afgerond",
        mode=ctx.mode,
        speaker_count=len(speakers),
        line_count=len(lines),
    )
    return lines if len(lines) > 1 else []


def section_highlights(data: Dict[str, Any], ctx: RenderContext) -> List[str]:
    log.debugx(
        "Markdown section_highlights gestart",
        mode=ctx.mode,
    )
    hl = data.get("highlights") or []
    if not isinstance(hl, list) or not hl:
        log.debugx(
            "Markdown section_highlights overgeslagen: geen highlights",
            mode=ctx.mode,
            highlights_type=type(hl).__name__,
        )
        return []

    lines = ["## ⭐ Highlights"]
    for h in hl:
        if not isinstance(h, dict):
            log.debugx(
                "Highlight overgeslagen: geen dict",
                highlight_type=type(h).__name__,
            )
            continue
        title = _s(h.get("title"))
        if not title:
            log.debugx(
                "Highlight overgeslagen: title ontbreekt",
                highlight_keys=list(h.keys()),
            )
            continue
        typ = _s(h.get("type"))
        evid = _s(h.get("evidence"))
        tr = fmt_timerange(h.get("start_s"), h.get("end_s"))

        log.debugx(
            "Highlight verwerken",
            mode=ctx.mode,
            title=title,
            type=typ,
            has_evidence=bool(evid),
            timerange=tr,
        )

        head = " · ".join([x for x in [(typ.upper() if typ else ""), tr] if x]).strip(" ·")
        lines.append(f"- **{title}**" + (f"  \n  _{head}_" if head else ""))

        if evid:
            if ctx.mode == "live":
                lines.append(f"  \n  > {evid[:240]}")
            else:
                lines.append(f"  \n  > “{evid}”")

    log.debugx(
        "Markdown section_highlights afgerond",
        mode=ctx.mode,
        highlight_count=len(hl),
        line_count=len(lines),
    )
    return lines if len(lines) > 1 else []


def section_action_items(data: Dict[str, Any], ctx: RenderContext) -> List[str]:
    log.debugx(
        "Markdown section_action_items gestart",
        mode=ctx.mode,
    )
    action_items = data.get("action_items") or data.get("todos") or []
    if not isinstance(action_items, list) or not action_items:
        log.debugx(
            "Markdown section_action_items overgeslagen: geen action items",
            mode=ctx.mode,
            action_items_type=type(action_items).__name__,
        )
        return []

    lines = ["## ✅ Action items"]

    done_states = {"done", "completed", "closed", "resolved", "finished", "afgerond", "klaar"}

    for t in action_items:
        if not isinstance(t, dict):
            log.debugx(
                "Action item overgeslagen: geen dict",
                action_item_type=type(t).__name__,
            )
            continue
        task = _s(t.get("task"))
        if not task:
            log.debugx(
                "Action item overgeslagen: task ontbreekt",
                action_item_keys=list(t.keys()),
            )
            continue

        tr = fmt_timerange(t.get("start_s"), t.get("end_s"))
        pr = _s(t.get("priority"))
        st_raw = _s(t.get("status")).lower()
        due = t.get("due")
        owner = t.get("owner")  # live shape
        owner_name = t.get("owner_name") or t.get("owner")  # final shape (owner can be name)
        owner_sid = t.get("owner_speaker_id")
        evidence = _s(t.get("evidence"))

        log.debugx(
            "Action item verwerken",
            mode=ctx.mode,
            task=task,
            priority=pr,
            status=st_raw,
            due=due,
            owner=owner,
            owner_name=owner_name,
            owner_speaker_id=owner_sid,
            has_evidence=bool(evidence),
            timerange=tr,
        )

        meta: List[str] = []
        if ctx.mode == "final":
            if owner_name:
                meta.append(f"**Owner:** {owner_name}")
            elif owner_sid:
                meta.append(f"**Owner:** {owner_sid}")
        else:
            if owner:
                meta.append(f"**Owner:** {owner}")

        if due:
            meta.append(f"**Due:** {due}")
        if pr:
            meta.append(f"**Prio:** {pr}")

        if st_raw:
            if ctx.mode == "final":
                is_done = st_raw in done_states
                if st_raw and not is_done and st_raw not in {"open", "new"}:
                    meta.append(f"**Status:** {st_raw}")
                checkbox = "[x]" if is_done else "[ ]"
            else:
                checkbox = "[ ]"
                if st_raw and st_raw != "new":
                    meta.append(f"**Status:** {st_raw}")
        else:
            checkbox = "[ ]"

        if tr:
            meta.append(tr)

        lines.append(f"- {checkbox} {task}" + (f"  \n  " + " · ".join(meta) if meta else ""))

        if evidence and ctx.mode == "final":
            lines.append(f"  \n  > _Evidence:_ “{evidence}”")

    log.debugx(
        "Markdown section_action_items afgerond",
        mode=ctx.mode,
        action_item_count=len(action_items),
        line_count=len(lines),
    )
    return lines if len(lines) > 1 else []


def section_decisions(data: Dict[str, Any], ctx: RenderContext) -> List[str]:
    log.debugx(
        "Markdown section_decisions gestart",
        mode=ctx.mode,
    )
    dl = data.get("decision_log") or []
    legacy = data.get("decisions") or []

    if ctx.mode == "live":
        if not isinstance(dl, list) or not dl:
            log.debugx(
                "Markdown section_decisions overgeslagen voor live: geen decision_log",
                mode=ctx.mode,
                decision_log_type=type(dl).__name__,
            )
            return []
        lines = ["## 🧠 Decisions"]
        for d in dl:
            if not isinstance(d, dict):
                log.debugx(
                    "Decision overgeslagen: geen dict",
                    decision_type=type(d).__name__,
                )
                continue
            dec = _s(d.get("decision"))
            if not dec:
                log.debugx(
                    "Decision overgeslagen: decision ontbreekt",
                    decision_keys=list(d.keys()),
                )
                continue
            tr = fmt_timerange(d.get("start_s"), d.get("end_s"))
            rat = _s(d.get("rationale"))
            owner = d.get("owner")
            extra: List[str] = []
            if rat:
                extra.append(rat)
            if owner:
                extra.append(f"Owner: {owner}")
            if tr:
                extra.append(tr)
            log.debugx(
                "Live decision verwerken",
                decision=dec,
                has_rationale=bool(rat),
                owner=owner,
                timerange=tr,
            )
            lines.append(f"- **{dec}**" + (f"  \n  _" + " · ".join(extra) + "_" if extra else ""))
        log.debugx(
            "Markdown section_decisions afgerond voor live",
            mode=ctx.mode,
            decision_count=len(dl),
            line_count=len(lines),
        )
        return lines if len(lines) > 1 else []

    # final
    if (not isinstance(dl, list) or not dl) and (not isinstance(legacy, list) or not legacy):
        log.debugx(
            "Markdown section_decisions overgeslagen voor final: geen decisions",
            mode=ctx.mode,
            decision_log_type=type(dl).__name__,
            legacy_type=type(legacy).__name__,
        )
        return []

    lines = ["## 🧠 Decisions"]
    if isinstance(dl, list) and dl:
        for d in dl:
            if not isinstance(d, dict):
                log.debugx(
                    "Decision overgeslagen: geen dict",
                    decision_type=type(d).__name__,
                )
                continue
            decision = _s(d.get("decision"))
            if not decision:
                log.debugx(
                    "Decision overgeslagen: decision ontbreekt",
                    decision_keys=list(d.keys()),
                )
                continue
            rationale = _s(d.get("rationale"))
            evidence = _s(d.get("evidence"))
            owner = d.get("owner")
            tr = fmt_timerange(d.get("start_s"), d.get("end_s"))

            log.debugx(
                "Final decision verwerken",
                decision=decision,
                has_rationale=bool(rationale),
                has_evidence=bool(evidence),
                owner=owner,
                timerange=tr,
            )

            meta: List[str] = []
            if owner:
                meta.append(f"**Owner:** {owner}")
            if tr:
                meta.append(tr)

            lines.append(f"- **{decision}**" + (f"  \n  " + " · ".join(meta) if meta else ""))
            if rationale:
                lines.append(f"  \n  _Rationale:_ {rationale}")
            if evidence:
                lines.append(f"  \n  > “{evidence}”")

    elif isinstance(legacy, list) and legacy:
        log.debugx(
            "Legacy decisions verwerken",
            legacy_count=len(legacy),
        )
        for d in legacy:
            if _is_nonempty_str(d):
                lines.append(f"- {d.strip()}")

    log.debugx(
        "Markdown section_decisions afgerond",
        mode=ctx.mode,
        line_count=len(lines),
    )
    return lines if len(lines) > 1 else []


def section_mind_map(data: Dict[str, Any], ctx: RenderContext) -> List[str]:
    log.debugx(
        "Markdown section_mind_map gestart",
        mode=ctx.mode,
    )
    # only in final (your live doesn't include)
    if ctx.mode != "final":
        log.debugx(
            "Markdown section_mind_map overgeslagen: alleen final mode",
            mode=ctx.mode,
        )
        return []
    mind_map = data.get("mind_map") or {}
    if not isinstance(mind_map, dict):
        log.debugx(
            "Markdown section_mind_map overgeslagen: mind_map is geen dict",
            mind_map_type=type(mind_map).__name__,
        )
        return []
    mm_format = _s(mind_map.get("format")).lower()
    mm_content = _s(mind_map.get("content"))
    if not mm_content:
        log.debugx(
            "Markdown section_mind_map overgeslagen: content ontbreekt",
            mm_format=mm_format,
        )
        return []

    lines = ["## 🗺️ Mind map"]
    if mm_format == "mermaid":
        log.debugx(
            "Mermaid mind map normaliseren",
            content_length=len(mm_content),
        )
        mm_content = normalize_mermaid_mindmap(mm_content)
        lines.append("```mermaid")
        lines.append(mm_content)
        lines.append("```")
    else:
        lines.append(mm_content)
    log.debugx(
        "Markdown section_mind_map afgerond",
        mode=ctx.mode,
        mm_format=mm_format,
        line_count=len(lines),
    )
    return lines


def section_sentiment(data: Dict[str, Any], ctx: RenderContext) -> List[str]:
    log.debugx(
        "Markdown section_sentiment gestart",
        mode=ctx.mode,
    )
    # only in live (your final doesn't include)
    if ctx.mode != "live":
        log.debugx(
            "Markdown section_sentiment overgeslagen: alleen live mode",
            mode=ctx.mode,
        )
        return []
    sent = data.get("sentiment") or {}
    if not isinstance(sent, dict):
        log.debugx(
            "Markdown section_sentiment overgeslagen: sentiment is geen dict",
            sentiment_type=type(sent).__name__,
        )
        return []
    overall = sent.get("overall")
    signals = sent.get("signals") or []
    conf = sent.get("confidence")

    if not overall and not signals:
        log.debugx(
            "Markdown section_sentiment overgeslagen: geen overall of signals",
            mode=ctx.mode,
        )
        return []

    lines = ["## 🙂 Sentiment"]
    if overall:
        lines.append(f"- **Overall:** {overall}" + (f" ({conf})" if conf else ""))
    for s in signals:
        if _is_nonempty_str(s):
            lines.append(f"- {s.strip()}")
    log.debugx(
        "Markdown section_sentiment afgerond",
        mode=ctx.mode,
        overall=overall,
        signal_count=len(signals) if isinstance(signals, list) else None,
        line_count=len(lines),
    )
    return lines if len(lines) > 1 else []


def section_supportive_questions(data: Dict[str, Any], ctx: RenderContext) -> List[str]:
    log.debugx(
        "Markdown section_supportive_questions gestart",
        mode=ctx.mode,
    )
    # only in live (your final doesn't include)
    if ctx.mode != "live":
        log.debugx(
            "Markdown section_supportive_questions overgeslagen: alleen live mode",
            mode=ctx.mode,
        )
        return []
    sq = data.get("supportive_questions") or []
    if not isinstance(sq, list) or not sq:
        log.debugx(
            "Markdown section_supportive_questions overgeslagen: geen supportive questions",
            mode=ctx.mode,
            supportive_questions_type=type(sq).__name__,
        )
        return []

    lines = ["## 💬 Supportive questions to ask"]
    for q in sq[:10]:
        if not isinstance(q, dict):
            log.debugx(
                "Supportive question overgeslagen: geen dict",
                question_type=type(q).__name__,
            )
            continue
        qq = _s(q.get("question"))
        why = _s(q.get("why"))
        when = _s(q.get("when_to_ask"))
        if not qq:
            log.debugx(
                "Supportive question overgeslagen: question ontbreekt",
                question_keys=list(q.keys()),
            )
            continue
        log.debugx(
            "Supportive question verwerken",
            question=qq,
            has_why=bool(why),
            has_when_to_ask=bool(when),
        )
        lines.append(
            f"- **{qq}**"
            + (f"  \n  _Why:_ {why}" if why else "")
            + (f"  \n  _When:_ {when}" if when else "")
        )
    log.debugx(
        "Markdown section_supportive_questions afgerond",
        mode=ctx.mode,
        question_count=len(sq),
        rendered_count=len(lines) - 1,
        line_count=len(lines),
    )
    return lines if len(lines) > 1 else []


def section_open_questions_and_notes(data: Dict[str, Any], ctx: RenderContext) -> List[str]:
    log.debugx(
        "Markdown section_open_questions_and_notes gestart",
        mode=ctx.mode,
    )
    # names differ between modes
    if ctx.mode == "live":
        oq = data.get("open_questions") or []
        notes = data.get("notes") or []
    else:
        oq = data.get("questions") or []
        notes = data.get("notes") or []

    lines: List[str] = []

    if isinstance(oq, list) and any(_is_nonempty_str(x) for x in oq):
        lines.append("## ❓ Open questions")
        for q in oq:
            if _is_nonempty_str(q):
                lines.append(f"- {q.strip()}")

    if isinstance(notes, list) and any(_is_nonempty_str(x) for x in notes):
        lines.append("## 📝 Notes")
        for n in notes:
            if _is_nonempty_str(n):
                lines.append(f"- {n.strip()}")

    log.debugx(
        "Markdown section_open_questions_and_notes afgerond",
        mode=ctx.mode,
        open_question_count=len(oq) if isinstance(oq, list) else None,
        note_count=len(notes) if isinstance(notes, list) else None,
        line_count=len(lines),
    )
    return lines


# -----------------------
# Default service instance
# -----------------------

log.debugx("Default markdown service initialiseren")
default_markdown_service = MarkdownRenderer().extend(
    [
        section_summary,
        section_key_points,
        section_speakers,
        section_highlights,
        section_action_items,
        section_decisions,
        section_mind_map,
        section_sentiment,
        section_supportive_questions,
        section_open_questions_and_notes,
    ]
)
log.debugx(
    "Default markdown service geïnitialiseerd",
    section_count=len(default_markdown_service._sections),
)

# Usage:
# md_final = default_markdown_service.render(payload, mode="final")
# md_live  = default_markdown_service.render(state_payload, mode="live")