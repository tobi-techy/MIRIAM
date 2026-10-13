# Secret scanning

This repo is public and runs on a free plan, so this is how design doc §11.6's secret-scanning requirement is met without any paid product.

## What GitHub gives for free, and what it does not

Per GitHub's docs ([about secret scanning](https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning), [about push protection](https://docs.github.com/en/code-security/secret-scanning/introduction/about-push-protection), checked 13 Oct 2026):

- **Free on public repos:** secret scanning alerts, and the partner-pattern reporting that comes with them. It only recognises token formats that service providers registered with GitHub. A credential in any other format (ChainRails keys, our own JWT/encryption keys, a generic `"name": ..., "value": ...` pair) raises no alert.
- **Free, but narrow:** push protection *for users* is on by default and covers only the pushing user's own pushes to public repos. It does not cover a bot, a collaborator who turned it off, or a commit made through the API.
- **Paid (GitHub Secret Protection):** push protection *for repositories*, the one that blocks any push to the repo. We do not use it and nothing here depends on it.

## What this repo uses instead

[gitleaks](https://github.com/gitleaks/gitleaks), pinned to v8.30.1, runs offline with no account. The pin is the release tag, plus the SHA-256 of the release tarball in `ci.yml` and the tag's commit SHA in `.pre-commit-config.yaml`.

- **CI** (`secret-scan` job in `.github/workflows/ci.yml`): scans the commits a push or pull request adds, where any finding fails the job. It then scans the history reachable from the checked-out commit and fails only on a finding that is not in `.gitleaks-baseline.json`. It prints rule, file:line, commit and date, never the value. Branches that are not an ancestor of the commit under test are not scanned by CI.
- **Locally** (`.pre-commit-config.yaml`): `pre-commit install` once, and the same gitleaks version and `.gitleaks.toml` scan staged changes before each commit. `git commit --no-verify` skips it, which is why CI is the check that blocks.
- **Config** (`.gitleaks.toml`): the stock rules plus one rule for secret-named `name`/`value` pairs, the shape ECS task definitions use. There are no path or regex allowlists on purpose.

## Handling a finding

Rotate the credential first. Then either remove it (history stays public, so rotation is what matters) or add it to the baseline in a pull request that a reviewer reads. A baseline entry needs a matching row in `docs/SECRET-SCANNING-BASELINE.md` (rule, path, why it is safe, and who confirmed the rotation); CI fails on an entry without one, so a leak cannot be hidden by baselining it quietly. The PR/range scan never uses the baseline.

```sh
gitleaks detect --config .gitleaks.toml --redact=100 --log-opts="--full-history HEAD" \
  --baseline-path .gitleaks-baseline.json --report-path new.json
# review new.json, then merge its entries into .gitleaks-baseline.json
```

The baseline is empty today: a full-history scan of this repo found nothing. gitleaks reads only the diff of non-merge commits, so a secret introduced only by resolving a merge conflict is not seen. It cannot tell whether a credential is still live.
