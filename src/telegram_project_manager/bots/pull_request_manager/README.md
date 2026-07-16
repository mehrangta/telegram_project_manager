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
the guarded code-job rebase flow, reruns CI for the rebased head, and resumes
the original merge or deploy operation. It makes at most two automatic rebase
attempts; unsafe conflicts or failed checks leave the pull request open and
send the admin retry controls.
