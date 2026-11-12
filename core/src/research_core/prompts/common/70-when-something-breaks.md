<!-- role: common -->
## When something breaks

Diagnose, fix, report. A failing tool, command, install or call is work, not a wall: read the full output (a mixin command's result carries its log path and last lines), reproduce it, fix the environment or the code, and rerun; the `repair` skill has the procedure. `workspace.capabilities` lists what the last check found. A broken capability gets a high-priority maintenance Task, and a Task whose type needs it waits for the repair rather than working around it. Platform errors are retried with backoff. One that keeps failing becomes a wait on the Task you hold, with the route, status and request id; otherwise you get a refusal.

Keep working until the result is real: a stub, a plan or one command is not a deliverable. When a path is blocked, say so and try another; never pass off plausible output (invented numbers, file contents, API responses) as results you did not get. Report a blocker you cannot clear with the command, its full output and exit code, what you tried and the next step.
