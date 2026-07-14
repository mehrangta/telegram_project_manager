# Pull Request Manager

The pull request manager handles `/deploy c-job_id` after a code job reaches
`ready`. It revalidates the exact checked PR head, squash-merges it to `main`
without bypassing branch protection, and watches the configured push-triggered
GitHub Actions deployment workflow. Merge and workflow state is persisted so
monitoring resumes after a bot restart.
