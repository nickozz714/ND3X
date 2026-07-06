# services/workflows/prompt_variable_resolver.py

from __future__ import annotations

import re

from component.logging import get_logger


log = get_logger(__name__)


TOKEN_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]*)")


class PromptVariableResolver:
    def __init__(self, *, repository, executor):
        log.infox(
            "PromptVariableResolver initialiseren",
            has_repository=repository is not None,
            repository_type=type(repository).__name__,
            has_executor=executor is not None,
            executor_type=type(executor).__name__,
        )
        self.repository = repository
        self.executor = executor
        log.infox("PromptVariableResolver geïnitialiseerd")

    def resolve(self, question: str) -> str:
        log.infox(
            "Prompt variables resolven gestart",
            question_length=len(question or ""),
            question_preview=(question or "")[:200],
        )

        tokens = sorted(set(TOKEN_RE.findall(question or "")))

        log.debugx(
            "Prompt variable tokens gevonden",
            token_count=len(tokens),
            tokens=tokens,
        )

        if not tokens:
            log.infox(
                "Prompt variables resolven overgeslagen: geen tokens gevonden",
                question_length=len(question or ""),
            )
            return question

        variables = self.repository.get_enabled_by_tokens(tokens)
        by_token = {variable.token: variable for variable in variables}

        log.infox(
            "Enabled prompt variables opgehaald",
            requested_token_count=len(tokens),
            requested_tokens=tokens,
            found_variable_count=len(variables or []),
            found_tokens=list(by_token.keys()),
            missing_tokens=[token for token in tokens if token not in by_token],
        )

        values: dict[str, str] = {}

        for token in tokens:
            variable = by_token.get(token)

            log.debugx(
                "Prompt variable verwerken",
                token=token,
                found=variable is not None,
            )

            # Unknown tokens stay as-is.
            if not variable:
                log.infox(
                    "Prompt variable onbekend, token blijft ongewijzigd",
                    token=token,
                )
                continue

            log.infox(
                "Prompt variable executor starten",
                token=token,
                variable_id=getattr(variable, "id", None),
                timeout_ms=variable.timeout_ms or 1000,
                code_length=len(variable.code or ""),
            )
            values[token] = self.executor.execute(
                code=variable.code,
                timeout_ms=variable.timeout_ms or 1000,
            )
            log.infox(
                "Prompt variable executor afgerond",
                token=token,
                variable_id=getattr(variable, "id", None),
                value_length=len(values.get(token) or ""),
            )

        def replace(match: re.Match) -> str:
            token = match.group(1)
            replacement = values.get(token, match.group(0))
            log.debugx(
                "Prompt variable token vervangen",
                token=token,
                replaced=token in values,
                replacement_length=len(replacement or ""),
            )
            return replacement

        result = TOKEN_RE.sub(replace, question)

        log.infox(
            "Prompt variables resolven afgerond",
            original_length=len(question or ""),
            result_length=len(result or ""),
            token_count=len(tokens),
            resolved_count=len(values),
            unresolved_tokens=[token for token in tokens if token not in values],
        )
        return result