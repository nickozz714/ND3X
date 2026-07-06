from __future__ import annotations

from typing import Any, Dict, List, Optional

from component.logging import get_logger
from services.assistants.orchestration.formatting import _as_list, _truncate_text


log = get_logger(__name__)


def build_doc(tool: str, tool_result: Any, kind: str) -> Optional[Dict[str, Any]]:
    log.infox(
        "Document bouwen uit tool resultaat gestart",
        tool=tool,
        kind=kind,
        tool_result_type=type(tool_result).__name__,
    )

    MAX_DOC_CONTENT_CHARS = 4000
    items = _as_list(tool_result)

    log.debugx(
        "Tool resultaat genormaliseerd naar lijst",
        tool=tool,
        kind=kind,
        item_count=len(items) if items is not None else None,
    )

    if not items:
        log.infox(
            "Document bouwen overgeslagen: geen items",
            tool=tool,
            kind=kind,
        )
        return None

    first = items[0]

    log.debugx(
        "Eerste tool resultaat geselecteerd",
        tool=tool,
        kind=kind,
        first_type=type(first).__name__,
        first_keys=list(first.keys()) if isinstance(first, dict) else None,
    )

    if tool == "code_search":
        log.debugx(
            "Code search document bouwen gestart",
            kind=kind,
            hit_count=len(items),
        )

        content = (first.get("code") or "").strip()
        if not content:
            log.infox(
                "Code search document bouwen overgeslagen: code content ontbreekt",
                kind=kind,
                hit_count=len(items),
                repository=first.get("repository"),
                file_path=first.get("file_path"),
            )
            return None

        repo = first.get("repository") or ""
        fp = first.get("file_path") or ""
        meta = f"{repo}/{fp}".strip("/") or (fp or "Code search hit")

        selected = {
            "repository": repo or None,
            "file_path": fp or None,
            "language": first.get("language"),
            "score": first.get("score"),
            "relevance": first.get("relevance"),
            "last_edit": first.get("last_edit"),
            "description": first.get("description"),
        }

        result = {
            "kind": kind,
            "meta": meta,
            "path": meta,
            "content": _truncate_text(content, MAX_DOC_CONTENT_CHARS),
            "hit_count": len(items),
            "selected": selected,
        }

        log.infox(
            "Code search document bouwen afgerond",
            kind=kind,
            meta=meta,
            repository=repo or None,
            file_path=fp or None,
            language=first.get("language"),
            hit_count=len(items),
            original_content_length=len(content),
            output_content_length=len(result["content"]),
            max_content_chars=MAX_DOC_CONTENT_CHARS,
        )
        return result

    if tool in ("text_search", "text__search"):
        log.debugx(
            "Text search document bouwen gestart",
            kind=kind,
            hit_count=len(items),
        )

        content = (first.get("file_content") or "").strip()
        if not content:
            log.infox(
                "Text search document bouwen overgeslagen: file_content ontbreekt",
                kind=kind,
                hit_count=len(items),
                doc_id=first.get("doc_id"),
                file_path=first.get("file_path"),
            )
            return None

        fp = first.get("file_path") or "Text search hit"
        meta = fp

        selected = {
            "doc_id": first.get("doc_id"),
            "file_path": first.get("file_path"),
            "score": first.get("score"),
            "source": first.get("source"),
            "embedding_id": first.get("embedding_id"),
        }

        result = {
            "kind": kind,
            "meta": meta,
            "path": fp,
            "doc_id": first.get("doc_id"),
            "content": _truncate_text(content, MAX_DOC_CONTENT_CHARS),
            "items": items,
            "selected": selected,
        }

        log.infox(
            "Text search document bouwen afgerond",
            kind=kind,
            meta=meta,
            path=fp,
            doc_id=first.get("doc_id"),
            source=first.get("source"),
            hit_count=len(items),
            original_content_length=len(content),
            output_content_length=len(result["content"]),
            max_content_chars=MAX_DOC_CONTENT_CHARS,
        )
        return result

    log.infox(
        "Document bouwen overgeslagen: tool wordt niet ondersteund",
        tool=tool,
        kind=kind,
        supported_tools=["code_search", "text_search"],
    )
    return None


def build_docs_for_tool_calls(tool_calls: List[Dict[str, Any]], tool_results: List[Any]) -> List[Dict[str, Any]]:
    log.infox(
        "Documenten bouwen voor tool calls gestart",
        tool_call_count=len(tool_calls or []),
        tool_result_count=len(tool_results or []),
    )

    docs: List[Dict[str, Any]] = []
    for call, result in zip(tool_calls, tool_results):
        tool = (call.get("tool") or "").strip()
        kind = (call.get("kind") or "").strip() or "markdown"

        log.debugx(
            "Tool call verwerken voor document build",
            tool=tool,
            kind=kind,
            call_keys=list(call.keys()) if isinstance(call, dict) else None,
            result_type=type(result).__name__,
            current_doc_count=len(docs),
        )

        if tool in ("code_search", "text_search", "text__search"):
            d = build_doc(tool, result, kind)
            if d:
                docs.append(d)
                log.debugx(
                    "Document toegevoegd vanuit tool call",
                    tool=tool,
                    kind=kind,
                    doc_count=len(docs),
                    meta=d.get("meta"),
                    path=d.get("path"),
                )
            else:
                log.debugx(
                    "Geen document toegevoegd vanuit tool call",
                    tool=tool,
                    kind=kind,
                )
        else:
            log.debugx(
                "Tool call overgeslagen voor document build",
                tool=tool,
                kind=kind,
            )

    log.infox(
        "Documenten bouwen voor tool calls afgerond",
        input_tool_call_count=len(tool_calls or []),
        input_tool_result_count=len(tool_results or []),
        doc_count=len(docs),
    )
    return docs


def build_doc_from_text_update_result(tool_result: Any) -> Optional[Dict[str, Any]]:
    log.infox(
        "Document bouwen uit text_update resultaat gestart",
        tool_result_type=type(tool_result).__name__,
    )

    if not isinstance(tool_result, dict):
        log.infox(
            "Document bouwen uit text_update resultaat overgeslagen: resultaat is geen dict",
            tool_result_type=type(tool_result).__name__,
        )
        return None

    if not tool_result.get("ok", False):
        log.infox(
            "Document bouwen uit text_update resultaat overgeslagen: resultaat niet ok",
            ok=tool_result.get("ok"),
            error=tool_result.get("error"),
            keys=list(tool_result.keys()),
        )
        return None

    doc = tool_result.get("doc")
    if not isinstance(doc, dict):
        log.infox(
            "Document bouwen uit text_update resultaat overgeslagen: doc ontbreekt of is geen dict",
            doc_type=type(doc).__name__,
            tool_result_keys=list(tool_result.keys()),
        )
        return None

    doc_id = doc.get("doc_id") or doc.get("id") or doc.get("docId")
    file_path = doc.get("file_path") or doc.get("path") or doc.get("filePath") or "Updated document"
    content = doc.get("content") or doc.get("file_content") or doc.get("text") or ""

    log.debugx(
        "Text update doc velden gelezen",
        doc_id=doc_id,
        file_path=file_path,
        content_type=type(content).__name__,
        content_length=len(content) if isinstance(content, str) else None,
        doc_keys=list(doc.keys()),
    )

    if not isinstance(content, str) or not content.strip():
        log.infox(
            "Document bouwen uit text_update resultaat overgeslagen: content ontbreekt",
            doc_id=doc_id,
            file_path=file_path,
            content_type=type(content).__name__,
        )
        return None

    result = {
        "kind": "markdown",
        "meta": str(file_path),
        "path": str(file_path),
        "doc_id": doc_id,
        "content": content,
        "hit_count": 1,
        "selected": {"doc_id": doc_id, "file_path": file_path},
        "source_tool": "text_update",
    }

    log.infox(
        "Document bouwen uit text_update resultaat afgerond",
        doc_id=doc_id,
        file_path=file_path,
        content_length=len(content),
        source_tool="text_update",
    )
    return result


def format_return_file_answer(*, tool_calls: List[Dict[str, Any]], tool_results: List[Any]) -> str:
    log.infox(
        "Return file antwoord formatteren gestart",
        tool_call_count=len(tool_calls or []),
        tool_result_count=len(tool_results or []),
    )

    blocks: List[str] = []
    for idx, (tc, tr) in enumerate(zip(tool_calls, tool_results), start=1):
        tool = (tc.get("tool") or "").strip()

        log.debugx(
            "Return file resultaat verwerken",
            index=idx,
            tool=tool,
            tool_call_keys=list(tc.keys()) if isinstance(tc, dict) else None,
            tool_result_type=type(tr).__name__,
            current_block_count=len(blocks),
        )

        if tool not in ("text_search", "code_search"):
            log.debugx(
                "Return file resultaat overgeslagen: tool niet relevant",
                index=idx,
                tool=tool,
            )
            continue

        hits = tr if isinstance(tr, list) else ([tr] if isinstance(tr, dict) else [])

        log.debugx(
            "Return file hits bepaald",
            index=idx,
            tool=tool,
            hit_count=len(hits),
        )

        if not hits:
            blocks.append(f"### Result {idx}\n_No results returned by **{tool}**._")
            log.infox(
                "Return file blok toegevoegd: geen resultaten",
                index=idx,
                tool=tool,
                block_count=len(blocks),
            )
            continue

        first = hits[0] if isinstance(hits[0], dict) else None
        if not first:
            blocks.append(f"### Result {idx}\n_No readable result returned by **{tool}**._")
            log.infox(
                "Return file blok toegevoegd: eerste resultaat niet leesbaar",
                index=idx,
                tool=tool,
                first_type=type(hits[0]).__name__ if hits else None,
                block_count=len(blocks),
            )
            continue

        if tool == "text_search":
            path = first.get("file_path") or "Text document"
            doc_id = first.get("doc_id")
            content = first.get("file_content") or ""
            head = f"### Result {idx}: {path}"
            if doc_id is not None:
                head += f" (doc_id={doc_id})"
            blocks.append(head + "\n\n" + (content.strip() or "_(empty)_"))

            log.infox(
                "Return file text_search blok toegevoegd",
                index=idx,
                path=path,
                doc_id=doc_id,
                content_length=len(content or ""),
                block_count=len(blocks),
            )

        if tool == "code_search":
            repo = first.get("repository") or ""
            fp = first.get("file_path") or "Code file"
            path = f"{repo}/{fp}".strip("/") or fp
            content = first.get("code") or ""
            lang = first.get("language") or ""
            blocks.append(f"### Result {idx}: {path}\n\n```{lang}\n{content.strip()}\n```")

            log.infox(
                "Return file code_search blok toegevoegd",
                index=idx,
                path=path,
                repository=repo or None,
                file_path=fp,
                language=lang,
                content_length=len(content or ""),
                block_count=len(blocks),
            )

    result = "\n\n---\n\n".join(blocks) if blocks else "No documents found."

    log.infox(
        "Return file antwoord formatteren afgerond",
        block_count=len(blocks),
        result_length=len(result),
        has_documents=bool(blocks),
    )
    return result