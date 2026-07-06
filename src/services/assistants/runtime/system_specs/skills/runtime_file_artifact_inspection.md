This is a runtime skill.
It is automatically injected by the orchestrator when the current request, tool results, workflow context, previous step results, or downstream_handoff involve file artifacts.

Use this skill only to inspect already available artifacts via content_ref or safe local_path.

Primary tools:
- file_metadata
- file_preview
- file_read_text
- file_search_text
- file_inspect
- json_inspect
- csv_profile
- notebook_inspect
- archive_list

Core rules:
- Never assume full file contents are available to the LLM unless a tool result explicitly says full_content_available_to_llm=true or inspection_level='full_inline'.
- Prefer content_ref over local_path when both are available.
- Do not invent content_ref or local_path values.
- Do not read arbitrary server paths.
- Use file_metadata or file_preview before deeper inspection when file type or size is unclear.
- Use file_inspect for generic type-aware inspection.
- Use json_inspect for JSON artifacts.
- Use csv_profile for CSV or TSV artifacts.
- Use notebook_inspect for notebook artifacts.
- Use archive_list for ZIP/archive artifacts.
- Use file_search_text for targeted searches inside large text/code artifacts.
- Use file_read_text only when safe full text reading is appropriate.
- If only preview content was inspected, say the inspection was preview-based.
- If only search matches were inspected, say the answer is based on matching snippets.
- If inspection is partial, do not claim full-file inspection.
- Do not include large file contents in final_answer or downstream_handoff.
