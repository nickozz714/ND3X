downstream_handoff is the canonical machine-readable result for later workflow steps.

When to include downstream_handoff:
- Include downstream_handoff when your result may be useful to a later assistant step.
- Include downstream_handoff for intermediate workflow work.
- Include downstream_handoff when extracted facts, artifact references, selected document IDs, file paths, URLs, names, or open questions should be reused.
- If no useful handoff is needed, set downstream_handoff to null.

Never use downstream_handoff as a substitute for a required mutation:
- If the current request requires creating, updating, deleting, saving, exporting, or overwriting a document/file, perform the required tool call before returning success.

Required downstream_handoff fields:
- summary: concise summary of the outcome.
- full_answer: full natural-language result when useful for later synthesis, otherwise null.
- artifacts: lightweight references to relevant files/docs/results.
- facts: structured reusable facts.
- iterables: named arrays for For-Each workflow operations. Each key is an iterable name and each value is an array of plain JSON objects; each object becomes the full input_payload for one child workflow run. Use {} when this step produces nothing to iterate over.
- open_questions: unresolved issues, if any.
- output_ref: always null; the orchestrator may replace large full_answer values with an external reference.
- status: one of success, partial, failed.

Keep downstream_handoff compact:
- Do not include full raw tool results.
- Do not include large search result lists.
- Do not include full document contents unless absolutely necessary.
- Do not duplicate evidence.
- Do not include internal reasoning.

Artifact references should be lightweight and may include:
- doc_id
- path
- file_path
- title
- source
- selected metadata
- created/updated/deleted status

Iterables rules:
- Populate iterables only when a later For-Each workflow operation should fan out over your results.
- Each iterable value must be an array of plain JSON objects; do not nest raw tool results or large blobs.
- Leave iterables as {} when there is nothing to iterate over.

Status rules:
- Use status='success' when the step completed fully.
- Use status='partial' when useful work was completed but limitations remain.
- Use status='failed' when the required outcome was not completed.

Failure rules:
- If a required tool call failed, include failure details compactly in downstream_handoff.status and facts.
- Do not claim success in summary when status is partial or failed.
