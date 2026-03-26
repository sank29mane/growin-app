# Plan: Sync with GitHub and Resolve PR Conflicts

## Objective
Sync the local repository with GitHub, resolve all conflicts in open Pull Requests, and merge them into `main`. Finally, sync the `gsd/v1.0-milestone` branch with the updated `main`.

## Tasks
- [ ] **Task 1: Fetch and Audit Remote State**
  - Fetch latest from `origin`.
  - Identify the sequence of PRs to merge (Oldest to Newest: 142, 149, 155, 156, 158, 159, 161, 163, 164, 165, 166, 167, 168, 169, 170, 176, 177, 178, 179).
- [ ] **Task 2: Merge Clean PRs into `main`**
  - Switch to `main` and pull.
  - Attempt to merge each PR.
  - Use `gh pr merge --squash --auto` for clean ones.
- [ ] **Task 3: Resolve Conflicting PRs**
  - For PRs that fail to merge, check out their branches.
  - Merge `main` into the PR branch.
  - Resolve conflicts (preferring `main` for project rules/state, but merging logic carefully).
  - Push the resolved branch and merge the PR.
- [ ] **Task 4: Sync Working Branch**
  - Switch back to `gsd/v1.0-milestone`.
  - Merge `main` into `gsd/v1.0-milestone`.
  - Resolve any final conflicts.

## Success Criteria
- [ ] All intended PRs are merged into `main`.
- [ ] `main` is synced with `origin`.
- [ ] `gsd/v1.0-milestone` is up-to-date with `main` and has no conflicts.
