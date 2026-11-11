# Confined reviewer on Ubuntu

`scripts/run-review.sh` uses `review-runner.py` to preflight the actual Codex
permission profile before spending an API call. A successful process exit does
not accept a round: the runner also checks the pinned HEAD, explicit verdict,
completed turn and successful shell inspection of the brief. It publishes the
response with an atomic no-clobber link. Rejected candidates and logs stay in
unique, gitignored `GPT_REVIEW_ATTEMPT_*` directories; retry the same round.
These checks establish execution and output completeness, not review quality.

The reviewer ignores user configuration except model and reasoning settings.
Project `.codex/config.toml` files are refused. Tool commands inherit no parent
environment secrets, have no network, and can read only runtime files, Codex's
installation, the repo and its resolved Git metadata. Existing environment files
and credential directories in the repo are masked at launch. Do not create new
secret files during a review: denials are a launch-time snapshot. Credentials
already committed in Git history cannot be hidden while granting Git reads.

On Ubuntu with restricted unprivileged namespaces, install the bwrap-specific
AppArmor profile after owner approval, from the repo root:

```bash
sudo install -m 0644 docs/operations/bwrap-userns-restrict /etc/apparmor.d/bwrap-userns-restrict
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
bwrap --ro-bind / / --unshare-user --unshare-net /usr/bin/true
```

Validate from the original failing terminal/service, not just a different launch
context. The profile grants `userns` to `/usr/bin/bwrap` for all callers of that
binary; Codex still applies read-only mounts, namespace isolation and seccomp.
Do not change global sysctls, setuid bits or bypass Codex's sandbox. The profile
is independently deployable from the review workflow changes.

The installer attempt on 2026-11-05 stopped at `sudo: a password is required`;
no host profile was installed by that attempt. Sandbox probes succeeded under
this session's `snap.zellij.zellij (complain)` label. Historical kernel records
from `unconfined` instead showed transition into `unprivileged_userns` followed
by `setpcap` and `net_admin` denials, explaining the loopback setup failure.
