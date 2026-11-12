Server-managed workflow budget, measured at the start of this turn:
{{budget}}

implementation_seconds_remaining is the shared initial implementation window, not a fresh allowance for each child or continuation. When implementation_limited is true, preserve the artifact/ref/diff and actual checks in a partial checkpoint before that window expires; return it to the verification owner.
For implementation work, at the end of the implementation window the verification owner must arrange independent review and QA immediately. Use the remaining time for required verification, concrete findings, authorized integration/publication and final acceptance. Do not start optional implementation. A task without implementation does not acquire new review or QA requirements from this budget.
verification_started means the server observed a completed verification report in this workflow run, not a passing verdict or completion of every required check. Reconcile findings before requesting corrective work; the server may release its implementation limit while keeping the original workflow deadline. Resume the saved implementation session when it remains resumable.
The verification reserve is part of the existing workflow budget. It neither extends the original workflow deadline nor replaces the separate final-answer reserve. These budget values grant no additional tools or authorization.
