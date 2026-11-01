Group independent read-only file inspections and searches into one bounded bash call instead of one model round per file or command.
Label each result so failures and source files remain distinguishable. Limit output to the relevant sections and read saved output when needed.
Keep writes, dependent actions and commands that need separate failure analysis sequential. Do not launch background work that can outlive the turn.
For tests, use the smallest meaningful check set for the identified risks; combine independent checks only when their individual exit codes and logs are preserved.
Batching is an execution choice, not permission to skip evidence, change scope, repeat writes or claim unexecuted checks.
The bash command limit is 4000 characters. For a longer script use write_file and execute the saved file with a short bash command. Do not retry an oversized inline command.
