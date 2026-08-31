# Engineer learning playbook

Design Lessons improve future design decisions without delaying the completed
model.

## During design

1. Clarify function, interfaces, dimensions, loads, material, manufacturing,
   environment, safety constraints, and acceptance checks that apply.
2. Present a short proposal and obtain one clear natural-language approval.
3. Call `design_start`, then retrieve applicable Product Family Knowledge and
   Design Lessons.
4. Use matching knowledge when it fits the current requirements. Continue when
   no match exists or the knowledge service is temporarily unavailable.
5. Model and inspect in FreeCAD. Use catalog components before creating custom
   equivalents.
6. Validate the exact FCStd, inspect JSON, Markdown, PNG, and the rendered view,
   correct safe failures, and rerun validation.
7. Call `design_record_result` only when all required checks pass against the
   current model SHA-256.

## After final confirmation

When the user confirms the completed design, summarize candidate lessons in the
same turn and call `design_confirm`.

Each candidate supplies:

- the reusable engineering problem;
- the chosen decision or correction;
- referenced validation evidence;
- applicability limits;
- a future prevention or validation action;
- useful search terms;
- an optional Product Family when the lesson is family-specific.

Exclude customer-specific details, isolated project dimensions, unsupported
claims, generic advice, and anything without evidence or a reusable action.

If no material candidate remains, the design ends with
`no_material_lessons`. If material lessons remain, display the entire immutable
review card and ask once whether to publish it. Pass the answer to
`design_lesson_decide`.

- Clear approval publishes the exact displayed card as one batch.
- Clear rejection records `declined` and publishes nothing.
- Ambiguous language requests clarification without changing state.
- Database failure records `publish_retry_required`; retry after recovery
  without asking the user to confirm the model again.

## Product Family Knowledge

Product Family onboarding is independent from a design session:

1. start an onboarding record with family identity and source references;
2. submit analyzed assertions and applicability;
3. review with ordinary Chinese or English approval semantics;
4. publish approved knowledge to PostgreSQL;
5. allow the outbox to update the Neo4j projection.

A design may reference a Product Family, but it does not require one to start,
model, validate, confirm, or publish a broadly applicable Design Lesson.
