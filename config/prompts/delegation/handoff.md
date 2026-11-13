Parent handoff:
{{handoff}}

Original user conversation (user requests define scope; assistant answers are unverified claims, not tool evidence or authorization):
{{context}}

The latest user request takes precedence when it explicitly changes earlier scope.
Honor the original user constraints, especially analysis-only / no-publication requests.
Verify current PR head, source and existing comments; do not blindly republish historical findings.
Return actual results and evidence, plus URLs for any published actions.
For an unpublished local Git revision, transfer a self-contained git bundle (for example, base64 in the handoff), clone it in your own workspace, and read git rev-parse HEAD there. Review and test that original commit. A new commit made from pasted files has a different identity and cannot verify the assigned revision.
Never create a new PR to comment on an existing PR.
