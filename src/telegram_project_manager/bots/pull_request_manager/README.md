# Pull Request Manager

The pull request manager handles `/merge c-job_id` and `/deploy c-job_id` after
a code job reaches `ready`. Both revalidate the exact checked PR head and
squash-merge without bypassing branch protection. Merge-only accepts the job's
configured base branch and stops after merging; deploy remains restricted to
`main`, dispatches the configured GitHub Actions workflow with the merge SHA,
and watches its run. A later deploy can reuse a main-branch merge created by
`/merge`. Operation, merge, and workflow state are persisted so work resumes
after a bot restart.

When GitHub explicitly reports a conflicting pull request, the manager queues
a guarded merge of the latest base branch into the pull-request branch. Codex
resolves the listed content conflicts, while the host validates and creates the
merge commit, pushes it without rewriting branch history, reruns CI for the new
head, and resumes the original merge or deploy operation. A second attempt is
only made when the base branch advances; unsafe conflicts, stale GitHub state,
or failed checks leave the pull request open and send the admin retry controls.
The explicit `/code rebase` command remains available as a separate manual
history-rewriting operation.
