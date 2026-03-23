# SHAgit
Scans GitHub org repos for Actions workflows and composite actions, rewrites mutable uses: owner/repo@tag refs to immutable full commit SHAs, preserves the original tag as a comment, and can open PRs with the changes. Skips local actions, docker refs, reusable workflows, archived repos, and empty repos.
