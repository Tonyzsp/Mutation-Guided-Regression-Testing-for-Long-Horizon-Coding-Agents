# Checkpoint-package release artifact

`rq1_full_36x1_checkpoint_packages.tar.xz` is the distributable Part A input for
the test-generation work. It is deliberately ignored by Git: at 132 MB it exceeds
GitHub's ordinary 100 MB per-file limit. Upload it as a GitHub Release asset named
`rq1-full-36x1-checkpoints` after pushing this repository commit.

| Field | Value |
| --- | --- |
| Archive | `rq1_full_36x1_checkpoint_packages.tar.xz` |
| Compression | tar + xz |
| Compressed size | 138,148,992 bytes (132 MiB) |
| Packages | 196 source-checkpoint packages across 36 tasks |
| SHA-256 | `eaf48ab1c7cb73e31d329cd49ba3d055f4f52b1571be605bb6fa8fa42301243a` |

Each package has this form:

```text
checkpoint_packages_rq1_full_36x1/<task>/C<n>/
  workspace/              # Agent code snapshot at C<n>
  specification.md        # Specification at C<n>
  base_tests/             # Official tests visible through C<n> only
  problem_config.yaml
  agent_run_manifest.json
  package.json            # Conforms to schemas/checkpoint_package.schema.json
```

This intentionally excludes evaluation reports, later test files, reference
solutions, raw agent conversation logs, and later checkpoint workspaces from each
individual package. A test generator working on `C<n>` must use only that package,
not a later `C<n+1>` directory.

## Download and extract

Download the archive from the GitHub Release, verify the SHA-256 shown above, then
extract it into the repository's ignored `data/checkpoints/` directory.

On Linux, macOS, or WSL:

```bash
sha256sum rq1_full_36x1_checkpoint_packages.tar.xz
mkdir -p data/checkpoints
tar -xJf rq1_full_36x1_checkpoint_packages.tar.xz -C data/checkpoints
```

On Windows, use 7-Zip to extract the `.xz` and then the resulting `.tar` into
`data\\checkpoints`. Do not commit the extracted directory.
