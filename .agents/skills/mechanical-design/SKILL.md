---
name: mechanical-design
description: Create or modify mechanical CAD through requirement clarification, one design-direction approval, knowledge retrieval, FreeCAD modeling, validation, final confirmation, and Design Lesson evaluation.
---

# Mechanical Design

Turn the request into explicit requirements and a short geometry proposal. Ask
for one natural-language decision on that direction. Treat clear Chinese or
English agreement as `APPROVE`, clear disagreement as `REJECT`, and ambiguous or
conditional language as `UNCLEAR`.

After approval:

1. call `design_start` and then `design_knowledge_retrieve`;
2. use matching knowledge when applicable and continue when none is available;
3. model directly in the session FCStd with FreeCAD or CadQuery;
4. run `freecad-model-validation`, inspect the JSON, Markdown, PNG, and visual
   result, correct safe failures, and rerun until required checks pass;
5. call `design_record_result` with the exact model and evidence paths;
6. when the user confirms the completed design, derive reusable candidate
   lessons and call `design_confirm` in the same turn;
7. finish if no material lesson exists, or display the returned review card and
   ask once whether to publish it; if the user corrects an unpublished pending
   card, pass their exact feedback as `review_revision_text` so a new immutable
   card supersedes the old card without overwriting it;
8. pass that natural-language publication decision to `design_lesson_decide`.

Final-model confirmation is independent from knowledge publication. A knowledge
outage must not invalidate a completed, exact-hash-validated model. Retry a
pending publication after recovery without asking the user to reconfirm the
model.

Treat existing source CAD as read-only. Preserve its snapshot and edit only the
session `model.FCStd`. Use `freecad-standard-parts` for catalog components and
`freecad-model-validation` after every visible model change.
