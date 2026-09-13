# Data Flow

## Creating work
Dashboard POST `/dashboard/api/work/create` -> `WorkService.create` -> lane
validated by NAME (`is_work_session`) BEFORE anything is written -> Work Policy
loaded and its binding recorded in run metadata -> `WorkStore.create_run` ->
plan written as REAL queue tasks.

## Dispatch
`work_loop` tick -> eligibility (`work_eligibility.evaluate`) -> the only
privileged action is `_set_dispatch`, which re-checks `is_work_session`.

## Completion evidence
Worker output -> `worker_output_after_prompt` strips everything up to and
including the instruction sentence -> marker matched only AFTER that point ->
queue state machine (RUNNING -> VERIFYING -> COMPLETED; there is no direct
RUNNING -> COMPLETED edge).

## Investigation (the token-saving order)
knowledge map -> git delta -> exact search -> narrow read -> widen only on
evidence. Encoded in `task_classifier.investigation_plan`.

## Confidence
`module_states()` -> one `git status` for the listing -> per module: working
tree beats commit history, because the file on disk is what will run.
