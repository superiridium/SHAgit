# 🔒 shagit --- Pin GitHub Actions to SHAs

Bulk convert GitHub Actions references from mutable tags (e.g. `@v4`) to
immutable full commit SHAs across an entire organization.

This helps enforce supply chain security best practices and prepares
your org for GitHub's **"require SHA pinning"** policy.

------------------------------------------------------------------------

## ✨ What it does

-   Scans all repos in a GitHub org (or selected repos)

-   Finds GitHub Actions usage in:

    -   `.github/workflows/*.yml`
    -   `.github/actions/**/action.yml`

-   Rewrites:

    ``` yaml
    uses: actions/checkout@v4
    ```

    into:

    ``` yaml
    uses: actions/checkout@<full-sha> # v4
    ```

-   Opens PRs with the changes (or runs in dry-run mode)

------------------------------------------------------------------------

## 🚫 What it does NOT touch

-   Local actions (`./something`)
-   Docker actions (`docker://...`)
-   Reusable workflows (`.github/workflows/...`)
-   Already pinned SHAs
-   Archived or empty repos

------------------------------------------------------------------------

## ⚙️ Requirements

-   Python 3.9+
-   gh (GitHub CLI)
-   git

Install GitHub CLI (macOS):

``` bash
brew install gh
```

Authenticate:

``` bash
gh auth login
```

------------------------------------------------------------------------

## 🚀 Usage

### Dry run (recommended first)

``` bash
python3 shagit.py your-org --dry-run
```

### Run against a single repo

``` bash
python3 shagit.py your-org --repo your-org/repo-name --dry-run
```

### Run for real (creates branches + PRs)

``` bash
python3 shagit.py your-org
```

### Override base branch

``` bash
python3 shagit.py your-org --base-branch master
```

------------------------------------------------------------------------

## 📊 Example output

    ==> org/repo (main)
      .github/workflows/build.yml: actions/checkout@v4 -> f43a0e5...

    ========================================================================
    SUMMARY
    ========================================================================
    Mode:              dry-run
    Repositories:      12
    Changed:           7
    No changes:        2
    Skipped:           2
    Failed:            1
    Files changed:     15
    Action refs pinned:28
    ========================================================================

------------------------------------------------------------------------

## 🔐 Why SHA pinning?

GitHub Actions tags (e.g. `@v4`) are mutable --- they can change over
time.

Full commit SHAs: - are immutable - prevent supply chain attacks - are
required for strict org-level security policies

------------------------------------------------------------------------

## 🔄 Keeping SHAs up to date

After migrating, enable Dependabot:

``` yaml
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
```

Dependabot will update pinned SHAs via PRs.

------------------------------------------------------------------------

## ⚠️ Notes

-   The script uses the GitHub API to resolve tags → SHAs
-   Handles repos with non-standard default branches
-   Safe to re-run (won't duplicate PRs)
-   Designed for org-wide bulk migration
