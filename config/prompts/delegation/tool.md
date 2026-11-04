Start an AX agent or continue its unfinished task with the same role and session_id.
Continuation restores that session's sandbox and tool context.
Durable tasks return immediately with a sessionId. Continue independent work, then use await_tasks when results are needed. Use depends_on for stages requiring successful direct-child results. Inspect outcome and evidence.
Give corrective instructions for remaining work; do not repeat uncertain external actions.
Give it the task and all required context explicitly.
Its workspace and conversation are separate.
Select by the role descriptions below.
For code review, send requirements, the diff and relevant source, test results, or a readable repository/ref/PR; a local path alone is insufficient.
Use its result in your final answer.
Available specialists:
