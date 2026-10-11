# Common Skill output contract

Status: normative contract. `skill-output.schema.json` is its
machine-readable form; this Markdown wins on a conflict.

This contract describes structured skill decision envelopes. The command-line
tools emit their documented artifacts — Index, EditPlan, detection arrays, and
reports — and do not automatically wrap every result in this schema. Validate
each artifact against its own documented format.

Each envelope has exactly one `output_kind`: `selection`, `proposal`,
`constraint`, `review`, or `failure`. It always carries a skill ID, confidence,
affected source/timeline scope, impact summary, and immutable evidence IDs.
`constraint` uses the EditPlan priority rules; `review` carries severity and
blocking; `failure` cannot be converted to an approval.

Dependencies explicitly state `required` and an `on_missing` behaviour:
`skip`, `empty_result`, or `escalate_review`. This is required even when an
optional input (for example alternate takes, music, or insert assets) is absent.
This makes dependency behaviour explicit without inventing missing inputs.

`backend_selections` uses the same closed record family as Index and EditPlan:
`purpose` (`stt`, `caption_renderer`, `ocr`, `shot_scene_boundary`, or `screen_activity`),
backend ID, version, and a resolved
runtime cost tier: `무료-로컬` (free, local execution), `쿼터` (quota-based usage),
or `유료` (paid usage). These are literal JSON values; use these strings exactly
as written. A Skill output may contain no backend selection when it did not make
a backend-produced observation. `혼합` (mixed usage) is a registry planning
classification, not a valid runtime selection.

If a Skill output raises a paid-call review, its existing `review` object may
carry `cost_approval`. That record is per call and includes the call ID,
expected cost, approver, and timestamp. The EditPlan is the sole execution
record: it links the call to `review(blocking)` and carries the plan total, so
this output does not create another approval mechanism.

Rationale: typed outputs distinguish selection, advice, constraint, review, and
failure. Explicit missing-input behaviour defines how dependencies are handled,
and blocking review semantics connect reviews to the rendering gate.
