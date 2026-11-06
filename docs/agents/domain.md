# Domain docs

## Before exploring

This repository uses a single-context layout.

- Read root `GLOSSARY.md` when it exists.
- Read ADRs in `docs/adr/` relevant to the area being explored.

If these documents do not exist, proceed silently. The `domain-modeling`
skill creates them when terms or decisions are resolved.

## Layout

- `GLOSSARY.md`: shared domain terms.
- `docs/adr/NNNN-title.md`: architecture decisions.

## Use the glossary vocabulary

Use defined terms in issue titles, proposals, hypotheses, and test names.
If a needed concept is missing, check whether an existing term fits;
record a real gap for `domain-modeling`.

## Flag ADR conflicts

If a proposal contradicts an ADR, identify that ADR and explain why
the decision should be reconsidered.
