# Pull Request Manager

The pull request manager handles `/deploy c-job_id` after a code job reaches
`ready`. It revalidates the exact checked PR head, squash-merges it to `main`
without bypassing branch protection, dispatches the configured GitHub Actions
workflow with the merge SHA, and watches its run. Merge and workflow state is persisted so
monitoring resumes after a bot restart.
