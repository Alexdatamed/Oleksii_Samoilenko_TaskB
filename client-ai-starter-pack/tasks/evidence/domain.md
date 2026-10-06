# Questionnaire review: exercise rules

These fictional rules are the source of truth for this assignment. No industry research is required.

1. Use only the supplied fictional product documents as evidence. An undocumented feature is unknown, not automatically supported or unsupported.
2. A document may replace another only through its explicit supersedes field. A newer date alone does not establish authority. Keep replaced text available to reviewers.
3. Only an approved answer can be reused. An unapproved edit is a draft. Mark an approved answer for review if a referenced document version changes.
4. Use the supplied topic-to-reviewer mapping. No knowledge of security standards, law, or compliance is required.

## Worked example

EXPORT-v1 allows CSV exports on every plan. EXPORT-v2 explicitly replaces it and limits CSV export to paid plans. The answer to “Can free-plan users export CSV?” is no, citing EXPORT-v2:p1 and showing the old conflict. JSON export is undocumented and needs review.

## Extend the starter

Keep the seeded policies and their authority metadata. Add questions and short passages consistent with those rules. Include supported, unsupported, conflicting-source, approved-reuse, and changed-source scenarios.

Record any unresolved ambiguity in your README. Do not silently add domain rules. Keep seed cases and their expected results so the reviewer can run the same checks.
