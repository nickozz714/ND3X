from __future__ import annotations

import re
from typing import Any, Tuple, Set, Optional, List

from component.logging import get_logger


log = get_logger(__name__)


_PLACEHOLDER_FULL = re.compile(r"^\s*\$\{([^}]+)\}\s*$")
_PLACEHOLDER_ANY = re.compile(r"\$\{([^}]+)\}")  # for interpolation inside strings

# Convert numeric bracket indices only: [0] -> .0
_BRACKET_INDEX = re.compile(r"\[(\d+)\]")

# Support a very small, controlled filter form:
#   items[?(@.name=='Data Platform')]
# NOTE: We intentionally only allow field names without dots, because dots inside the
# filter (e.g. @.a.b) would complicate path splitting and is not needed for your use-case.
_SIMPLE_FILTER = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\[\?\(@\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)==['\"](?P<value>[^'\"]+)['\"]\)\]$"
)


def _split_path(path: str) -> List[str]:
    """
    Split a path by '.' but DO NOT split inside brackets [...].

    Example:
      items[?(@.name=='X')].id  -> ["items[?(@.name=='X')]", "id"]
      items[0].id               -> ["items[0]", "id"]
      items.0.id                -> ["items", "0", "id"]
    """
    log.debugx(
        "Placeholder path splitten gestart",
        path=path,
        path_length=len(path or ""),
    )

    parts: List[str] = []
    buf: List[str] = []
    bracket_depth = 0

    for ch in (path or ""):
        if ch == "." and bracket_depth == 0:
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
            continue

        if ch == "[":
            bracket_depth += 1
        elif ch == "]" and bracket_depth > 0:
            bracket_depth -= 1

        buf.append(ch)

    seg = "".join(buf).strip()
    if seg:
        parts.append(seg)

    log.debugx(
        "Placeholder path splitten afgerond",
        path=path,
        parts=parts,
        part_count=len(parts),
        final_bracket_depth=bracket_depth,
    )
    return parts


def _normalize_path(path: str) -> str:
    """
    Normalize common planner outputs to the dot-segment format we support:
      - items[0].id  -> items.0.id
      - items.[0].id -> items.0.id

    IMPORTANT:
    - We ONLY rewrite numeric indices [N].
    - Filter brackets like items[?(@.name=='X')] are left untouched.
    """
    log.debugx(
        "Placeholder path normaliseren gestart",
        path=path,
    )

    if not path:
        log.debugx("Placeholder path normaliseren overgeslagen: lege path")
        return path

    original = path

    # remove ".[" -> "[" so "items.[0]" becomes "items[0]"
    path = path.replace(".[", "[")

    # convert [N] to .N (numeric only)
    path = _BRACKET_INDEX.sub(r".\1", path)

    # collapse accidental double dots
    while ".." in path:
        path = path.replace("..", ".")

    result = path.strip(".")

    log.debugx(
        "Placeholder path normaliseren afgerond",
        original_path=original,
        normalized_path=result,
        changed=original != result,
    )
    return result


def _get_by_path(root: Any, path: str) -> Any:
    """
    Safe traversal via dot-separated path, with dynamic fallback.
    Extended to support:
      - bracket numeric indices: foo[0]  (normalized to foo.0)
      - simple list filter: items[?(@.name=='X')]  (returns first matching element)
      - mild aliasing: if key 'items' missing but 'lists' exists, treat 'lists' as 'items'

    Notes:
    - "Safe": returns None when not found.
    - Guards against cycles and runaway recursion.
    """
    log.infox(
        "Placeholder path resolven gestart",
        root_type=type(root).__name__,
        path=path,
    )

    path = _normalize_path(path or "")
    parts = [p.strip() for p in _split_path(path) if p.strip()]
    if not parts:
        log.debugx(
            "Placeholder path resolven afgerond: geen parts, root teruggeven",
            path=path,
            root_type=type(root).__name__,
        )
        return root

    MAX_DEPTH = 25

    def resolve_direct(cur: Any, seg: str) -> Tuple[bool, Any]:
        """Try resolving seg from cur without any fallback search."""
        log.debugx(
            "Placeholder segment direct resolven",
            current_type=type(cur).__name__,
            segment=seg,
        )

        # support simple filter segment like: items[?(@.name=='Data Platform')]
        m = _SIMPLE_FILTER.match(seg)
        if m and isinstance(cur, dict):
            key = m.group("key")
            field = m.group("field")
            value = m.group("value")

            arr = cur.get(key)
            if arr is None and key == "items" and "lists" in cur:
                arr = cur.get("lists")
                log.debugx(
                    "Placeholder simple filter gebruikt alias lists voor items",
                    key=key,
                    field=field,
                    value=value,
                )

            if isinstance(arr, list):
                for el in arr:
                    if isinstance(el, dict) and el.get(field) == value:
                        log.debugx(
                            "Placeholder simple filter match gevonden",
                            key=key,
                            field=field,
                            value=value,
                            matched_keys=list(el.keys()),
                        )
                        return True, el
            log.debugx(
                "Placeholder simple filter geen match",
                key=key,
                field=field,
                value=value,
                arr_type=type(arr).__name__,
                arr_len=len(arr) if isinstance(arr, list) else None,
            )
            return False, None

        if isinstance(cur, dict):
            if seg in cur:
                log.debugx(
                    "Placeholder dict segment gevonden",
                    segment=seg,
                    value_type=type(cur[seg]).__name__,
                )
                return True, cur[seg]

            # mild alias: items <-> lists (helps todo_list_list vs planner items)
            if seg == "items" and "lists" in cur:
                log.debugx("Placeholder dict segment alias gebruikt: items -> lists")
                return True, cur["lists"]
            if seg == "lists" and "items" in cur:
                log.debugx("Placeholder dict segment alias gebruikt: lists -> items")
                return True, cur["items"]

            log.debugx(
                "Placeholder dict segment niet gevonden",
                segment=seg,
                available_keys=list(cur.keys())[:20],
            )
            return False, None

        if isinstance(cur, list):
            if seg.isdigit():
                i = int(seg)
                if 0 <= i < len(cur):
                    log.debugx(
                        "Placeholder list index gevonden",
                        index=i,
                        list_len=len(cur),
                        value_type=type(cur[i]).__name__,
                    )
                    return True, cur[i]
            log.debugx(
                "Placeholder list segment niet gevonden",
                segment=seg,
                list_len=len(cur),
            )
            return False, None

        log.debugx(
            "Placeholder segment direct resolven niet mogelijk voor type",
            segment=seg,
            current_type=type(cur).__name__,
        )
        return False, None

    def search_for_segment(cur: Any, seg: str, *, depth: int, seen: Set[int]) -> Optional[Any]:
        """
        Fallback: find a nested object within cur from which seg can be resolved directly.
        Returns the resolved value for seg, or None if not found.
        """
        log.debugx(
            "Placeholder fallback search gestart",
            segment=seg,
            depth=depth,
            current_type=type(cur).__name__,
            seen_count=len(seen),
        )

        if depth <= 0:
            log.debugx(
                "Placeholder fallback search gestopt: max depth bereikt",
                segment=seg,
            )
            return None

        oid = id(cur)
        if oid in seen:
            log.debugx(
                "Placeholder fallback search gestopt: cycle gedetecteerd",
                segment=seg,
                object_id=oid,
            )
            return None
        seen.add(oid)

        # If cur itself can resolve seg, great.
        ok, nxt = resolve_direct(cur, seg)
        if ok:
            log.debugx(
                "Placeholder fallback search vond segment direct",
                segment=seg,
                value_type=type(nxt).__name__,
            )
            return nxt

        # Otherwise, search deeper.
        if isinstance(cur, dict):
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    found = search_for_segment(v, seg, depth=depth - 1, seen=seen)
                    if found is not None:
                        log.debugx(
                            "Placeholder fallback search vond segment in dict child",
                            segment=seg,
                            value_type=type(found).__name__,
                        )
                        return found

        elif isinstance(cur, list):
            for el in cur:
                if isinstance(el, (dict, list)):
                    found = search_for_segment(el, seg, depth=depth - 1, seen=seen)
                    if found is not None:
                        log.debugx(
                            "Placeholder fallback search vond segment in list child",
                            segment=seg,
                            value_type=type(found).__name__,
                        )
                        return found

        log.debugx(
            "Placeholder fallback search geen resultaat",
            segment=seg,
            depth=depth,
        )
        return None

    cur = root
    for seg in parts:
        log.debugx(
            "Placeholder path segment verwerken",
            path=path,
            segment=seg,
            current_type=type(cur).__name__,
        )

        ok, nxt = resolve_direct(cur, seg)
        if ok:
            cur = nxt
            continue

        # Dynamic fallback: look inside cur to find seg somewhere nested
        found = search_for_segment(cur, seg, depth=MAX_DEPTH, seen=set())
        if found is None:
            log.infox(
                "Placeholder path resolven mislukt: segment niet gevonden",
                path=path,
                segment=seg,
                root_type=type(root).__name__,
            )
            return None
        cur = found

    log.infox(
        "Placeholder path resolven afgerond",
        path=path,
        result_type=type(cur).__name__,
        result_preview=str(cur)[:200],
    )
    return cur


def _resolve_expr(expr: str, *, results: List[Any]) -> Any:
    """
    Supported:
      - result.<i>.<path...>
      - last.<path...>
      - last  (whole object)
      - coalesce with || :
          ${result.2.items[?(@.name=='X')].id || result.2.items.0.id}
    """
    log.infox(
        "Placeholder expressie resolven gestart",
        expr=expr,
        result_count=len(results or []),
    )

    expr = (expr or "").strip()
    if not expr:
        log.debugx("Placeholder expressie resolven overgeslagen: lege expressie")
        return None

    # support coalesce: a || b || c
    if "||" in expr:
        log.debugx(
            "Placeholder coalesce expressie gedetecteerd",
            expr=expr,
            part_count=len(expr.split("||")),
        )
        for part in expr.split("||"):
            part = part.strip()
            if not part:
                continue
            val = _resolve_expr(part, results=results)
            if val is not None:
                log.infox(
                    "Placeholder coalesce expressie opgelost",
                    expr=expr,
                    selected_part=part,
                    value_type=type(val).__name__,
                )
                return val
        log.infox(
            "Placeholder coalesce expressie gaf geen resultaat",
            expr=expr,
        )
        return None

    if expr == "last":
        result = results[-1] if results else None
        log.infox(
            "Placeholder expressie last opgelost",
            has_results=bool(results),
            result_type=type(result).__name__,
        )
        return result

    if expr.startswith("last."):
        result = _get_by_path(results[-1] if results else None, expr[len("last."):])
        log.infox(
            "Placeholder expressie last.path opgelost",
            expr=expr,
            has_results=bool(results),
            result_type=type(result).__name__,
        )
        return result

    if expr.startswith("result."):
        rest = expr[len("result."):]
        # rest begins with "<i>." or "<i>"
        parts = rest.split(".", 1)
        if not parts[0].isdigit():
            log.infox(
                "Placeholder result expressie ongeldig: index is niet numeriek",
                expr=expr,
                index_part=parts[0] if parts else None,
            )
            return None
        i = int(parts[0])
        if i < 0 or i >= len(results):
            log.infox(
                "Placeholder result expressie buiten bereik",
                expr=expr,
                index=i,
                result_count=len(results),
            )
            return None
        if len(parts) == 1:
            result = results[i]
            log.infox(
                "Placeholder result expressie opgelost naar volledig resultaat",
                expr=expr,
                index=i,
                result_type=type(result).__name__,
            )
            return result
        result = _get_by_path(results[i], parts[1])
        log.infox(
            "Placeholder result.path expressie opgelost",
            expr=expr,
            index=i,
            path=parts[1],
            result_type=type(result).__name__,
        )
        return result

    log.infox(
        "Placeholder expressie niet ondersteund",
        expr=expr,
    )
    return None


def resolve_placeholders(obj: Any, *, results: List[Any]) -> Any:
    """
    Recursively resolves placeholders in dict/list/str.
    - If a string is exactly "${...}", it returns the resolved value (preserving type).
    - If a string contains "${...}" inside other text, it interpolates into a string.
    """
    log.debugx(
        "Placeholders resolven gestart",
        object_type=type(obj).__name__,
        result_count=len(results or []),
    )

    if obj is None:
        log.debugx("Placeholders resolven: None blijft None")
        return None

    if isinstance(obj, dict):
        log.debugx(
            "Placeholders resolven voor dict",
            key_count=len(obj),
            keys=list(obj.keys())[:20],
        )
        return {k: resolve_placeholders(v, results=results) for k, v in obj.items()}

    if isinstance(obj, list):
        log.debugx(
            "Placeholders resolven voor list",
            item_count=len(obj),
        )
        return [resolve_placeholders(v, results=results) for v in obj]

    if isinstance(obj, str):
        s = obj

        # whole-string placeholder -> preserve type
        m = _PLACEHOLDER_FULL.match(s)
        if m:
            expr = m.group(1)
            log.debugx(
                "Volledige placeholder string gedetecteerd",
                expr=expr,
            )
            val = _resolve_expr(expr, results=results)
            result = val if val is not None else obj
            log.debugx(
                "Volledige placeholder string opgelost",
                expr=expr,
                resolved=val is not None,
                result_type=type(result).__name__,
            )
            return result

        # interpolation -> always string
        def _sub(match: re.Match) -> str:
            expr = match.group(1)
            val = _resolve_expr(expr, results=results)
            log.debugx(
                "Placeholder interpolatie segment opgelost",
                expr=expr,
                resolved=val is not None,
                value_type=type(val).__name__,
            )
            return "" if val is None else str(val)

        if "${" in s:
            log.debugx(
                "Placeholder interpolatie string gedetecteerd",
                text_length=len(s),
            )
            result = _PLACEHOLDER_ANY.sub(_sub, s)
            log.debugx(
                "Placeholder interpolatie string afgerond",
                original_length=len(s),
                result_length=len(result),
            )
            return result

        log.debugx("String zonder placeholders blijft ongewijzigd")
        return obj

    # ints/bools/etc untouched
    log.debugx(
        "Placeholders resolven: object type blijft ongewijzigd",
        object_type=type(obj).__name__,
    )
    return obj


_PLACEHOLDER_RESULT_INDEX = re.compile(r"\$\{\s*result\.(\d+)\b")

def _find_placeholder_result_indices(obj: Any) -> set[int]:
    log.debugx(
        "Placeholder result indices zoeken gestart",
        object_type=type(obj).__name__,
    )
    out: set[int] = set()
    if isinstance(obj, dict):
        for v in obj.values(): out |= _find_placeholder_result_indices(v)
    elif isinstance(obj, list):
        for v in obj: out |= _find_placeholder_result_indices(v)
    elif isinstance(obj, str):
        for m in _PLACEHOLDER_RESULT_INDEX.finditer(obj):
            out.add(int(m.group(1)))
    log.debugx(
        "Placeholder result indices zoeken afgerond",
        object_type=type(obj).__name__,
        indices=sorted(out),
    )
    return out