Use review_checkpoint to keep covered files, accepted/rejected candidates with reasons, and concrete remaining work explicit.
Every six work rounds the runtime requests only this checkpoint.
Do not reopen rejected candidates without new evidence.
If nothing remains, record remaining_work=[]; this ends tool use.
Include outstanding publication and verification in remaining_work.
A successful CI job is not proof that a specific test ran without skips: inspect its test report/log, or state that execution is unverified.
