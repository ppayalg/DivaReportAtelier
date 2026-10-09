# Configuration schema

`diva-atelier validate configs/*.json` checks the structural rules below (ids, hierarchy, variables, placeholders, sources, weights). Input values are checked per report by the planner. The HTTP API runs the same
validation on `PUT /api/configs/<key>` and rejects invalid configurations with HTTP 422.

## Top level

| Field | Type | Notes |
| --- | --- | --- |
| `id`, `name`, `version`, `status` | string / int | `status` is `Draft` or `Active`. |
| `type`, `client`, `country` | string | Report type (Bridal Gowns...), label (Bridal Atelier...), ISO-style country code. |
| `output`, `template`, `style`, `desc` | string | Output format (`docx`, `pdf`, `html`) and presentation metadata. |
| `numApproversRequired` | int ≥ 1 | Approvals needed at the global stage. |
| `globalApprovers` | `[{role, sla, order, required}]` | Named roles that must sign off, in `order`. `sla` is hours. |
| `autoApproveThreshold` | number, optional | QA score (0-100) at which auto-approve skips review. Default 90. |
| `consumedFeedback` | string[] | Maintained by Insights; feedback already acted on. |
| `variables`, `sections` | arrays | See below. |

## Variables

`{id, name, label, type, sort, required, help, options, validation}`

* `name` is snake_case and unique. It is referenced as `{name}` in instructions and queries.
* `type`: `FreeText`, `Selection`, `MultiSelection`, `Date`, `DateRange`, `Boolean`, `Number`, `Collection`.
* `Selection` and `MultiSelection` need `options`.
* `DateRange` values are `{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}`. `validation.maxSpanDays`
  and `validation.maxDate: "today"` are honoured.

## Sections

`{id, num, title, type, shape, seq, parent, required, generatable, instructions, sources, approvers,
condition, styleTone, qualityConfig, sampleHtml}`

* `parent` is a section `id` (empty for top level). Cycles and missing parents are errors.
* `instructions` may use `{variables}`; undefined ones are errors. `writingInstructions` is accepted
  as an alias.
* `shape`: `Narrative`, `Text`, `Table`, `DataTable`, `Figure`, `Mixed`, `Appendix`. Table shapes
  must contain a Markdown table or QA fails the draft.
* `generatable: false` sections are left for a person (`manual`).
* `condition`: `{variable, operator, value}` with operator `equals`, `notEquals`, `in`, `notIn`,
  `isNotNull`, `isEmpty`. `true`, `Yes` and `yes` compare equal.

### `sources[]`

`{id, priority, index, query, mode, topK, minScore, filter | contentClass, fallback, isRequired}`

* `mode`: `Semantic`, `Keyword`, `Hybrid`, `MetadataOnly`. `minScore` is 0-1. `topK` ≥ 1.
* `filter` / `contentClass`: `Narrative`, `Table`, `Image`, `RawDocument`.
* `fallback`: `GenerateWithoutContext`, `Warn`, `Skip`, `UseDefault`, `Escalate`.

### `styleTone`

`{voice, formality, tense, person, citationStyle, maxParagraphs, preferredTerminology}`.
`preferredTerminology` is a `{"avoid": "prefer"}` map applied whole-word and case-aware to every draft.

### `qualityConfig`

`{minScore (0-100, default 90), maxRevisions (default 3), criticalIssueThreshold (default 0),
qaInstructions, validationChecklist[], scoringWeights}`.
`scoringWeights` covers `accuracy`, `completeness`, `structure`, `clarity`, `compliance`, must sum to
1.0 when given, and defaults to equal weights.

Scores: each dimension is 0-1, weighted into 0-100, then reduced by 30 per critical issue, 8 per
major and 2 per minor. A section passes at `score >= minScore` with no more than
`criticalIssueThreshold` critical issues.

### `approvers[]`

`{role, sla, order, required}` per section. Requests run in `order`.
