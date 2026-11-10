# Issue tracker: GitHub

Issues and published specs live in GitHub Issues for `YauheniPo/popot-agents`.
Use the `gh` CLI from inside this clone.

Before the first write of a session, run `gh repo view --json nameWithOwner`
and verify the target is `YauheniPo/popot-agents`. If it differs, correct the
target before writing.

## Conventions

- Create: `gh issue create --title "..." --body-file <file>`.
- Read: `gh issue view <number> --json number,title,body,labels,comments`.
- List: `gh issue list --state open --json number,title,body,labels,comments`.
  Add `--label` filters as needed.
- Edit a body: `gh issue edit <number> --body-file <file>`.
- Comment: `gh issue comment <number> --body-file <file>`.
- Apply or remove labels: `gh issue edit <number> --add-label "..."` or
  `gh issue edit <number> --remove-label "..."`.
- Create a sub-issue: `gh issue create --parent <parent> --title "..." --body-file <file>`.
  Link an existing child with `gh issue edit <parent> --add-sub-issue <child>`.
- Blocking: use native dependencies:
  `gh issue edit <child> --add-blocked-by <blocker>`.
  When creating an issue, use `--blocked-by <number>,<number>`.
- Count open blockers:
  `gh api repos/YauheniPo/popot-agents/issues/<number> --jq .issue_dependencies_summary.blocked_by`.
  Zero means no open blockers.
- If native relationships are unavailable, put `Part of #<parent>` or
  `Blocked by: #<number>, #<number>` at the top of the child body.
  A ticket is unblocked when every blocker is closed.
- Close: post the explanation with `gh issue comment <number> --body-file <file>`,
  then run `gh issue close <number>`. For an already-closed issue, post the
  comment separately; `gh issue close --comment` can omit it.

Use files for multiline bodies.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Skill language

- "Publish to the issue tracker": create a GitHub issue.
- "Fetch the relevant ticket": read the GitHub issue and its comments.
