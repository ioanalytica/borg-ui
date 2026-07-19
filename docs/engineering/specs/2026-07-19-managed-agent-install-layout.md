# Managed Agent Install Layout Spec

Status: target picture, agreed before implementation. Supersedes parts of
`2026-05-22-borg-installer-management.md` regarding how Borg reaches the node.

## Problem

The installer served at `GET /agent/install.sh` gives a node software that does
not match the server it enrolls against.

| Component | Server image | Node today |
| --- | --- | --- |
| Borg 1 | `1.4.4`, pinned | `apt-get install borgbackup` — 1.2.8 on Ubuntu 24.04 |
| Borg 2 | `2.0.0b21`, pinned | `pip install --pre "borgbackup>=2.0.0b1,<3"` — unpinned |
| Agent | built from this tree | `pip install git+https://github.com/…@main` |

Borg 2 is the acute case: two nodes installed on different days get different
betas against one server, and repository format changes between betas. The
agent case matters for anyone whose server runs ahead of `main` — the node must
get the agent that belongs to *its* server, not whatever `main` holds today.

Only the server knows the answer to "which versions belong here", so the server
must be the one to say it.

## Delivery Mechanism

Borg publishes no wheels on PyPI — only sdists, for both 1.4.4 and 2.0.0b21.
Any pip-based install therefore compiles from source and needs a build
toolchain on every node. Borg does publish static single-file binaries per
release, and the relevant Linux matrix is small:

| Release | Variants |
| --- | --- |
| 1.4.4 | `glibc231-x86_64`, `glibc235-x86_64`, `glibc235-arm64` |
| 2.0.0b21 | `glibc235-x86_64`, `glibc235-arm64` |

Static binaries are how a node gets an exact version without a compiler. The
agent itself stays a venv install: it is pure Python (`requests`,
`websocket-client`) and pip is needed for those dependencies anyway.

The agent does not use `borg mount`, so the absence of FUSE support in a static
binary is not a constraint.

Gaps with no static binary — 32-bit ARM (Raspberry Pi OS 32-bit) and musl
(Alpine) — must fail with an explicit message pointing at the distro fallback,
not with a download error.

## Node Layout

```
/opt/borg-ui-agent/borg1/<version>/borg   static binary, root:root
/opt/borg-ui-agent/borg2/<version>/borg   static binary, root:root
/opt/borg-ui-agent/bin/borg1, borg2       forwarder scripts, root:root 0755
/opt/borg-ui-agent/.venv/                 agent venv (pure Python)
/usr/local/bin/borg, borg2                symlinks to the forwarders
/etc/borg-ui-agent/config.toml            0600, owned by the service user
```

Version-scoped directories allow an upgrade to land beside the running version
instead of replacing it.

The agent resolves Borg through `PATH` — `agent/borg_ui_agent/borg.py` calls
`shutil.which("borg")` and there is no configuration key for a binary path. The
`/usr/local/bin` entries are therefore **the mechanism, not a convenience**:
`/usr/local/bin` precedes `/usr/bin`, which is what makes the agent run the
pinned binary rather than the distribution's. Shadowing a distribution `borg`
is consequently required rather than optional, and the installer only steps
aside when something that is not a symlink already occupies the path — in which
case it says so, because version parity is then lost.

The forwarder is a script rather than a symlink because it is the one place
that can later carry policy — exit-code handling, or elevation — without
touching callers. It lives under `/opt/borg-ui-agent` and `/usr/local/bin`
holds a symlink to it, so that `_classify_install_source()` keeps resolving
through to an installer-managed path and reporting `borg-ui-installer`.

Forwarders carry the generic wrapper behaviour only: `BORG_VERSION` dispatch,
and keeping stdout and stderr separate because Borg UI parses `--json` on
stdout and reads stderr for warnings. Deployment-specific logic (`--remote-path`
injection, a fixed `/usr/bin/borg` target) does not belong here. Downgrading a
Borg warning exit code to success is useful but is a policy choice and should be
opt-in rather than default.

**The forwarders contain no `sudo`.** Unprivileged users on the machine can run
`borg` for their own files with their own permissions; that is intended. Putting
`sudo` in a world-executable forwarder would grant escalation to every caller,
not only to the agent.

## Privilege Model

Backing up everything requires reading files the service user does not own. The
minimal mechanism for exactly that is a capability on the unit:

```
AmbientCapabilities=CAP_DAC_READ_SEARCH
```

This grants "may read any file" and nothing else — no writing, no `chown`, no
command execution. It inherits to the Borg child process, is scoped to the
service rather than to a binary anyone can execute, and is compatible with
`NoNewPrivileges=true`, which stays set.

Restore to the original location is the only operation needing more (write,
`chown`), and is out of scope for the first implementation. When it is built it
must be an explicit, separately chosen install option, because:

- `sudo` cannot work while `NoNewPrivileges=true` is set, so enabling it
  weakens the unit's baseline;
- a sudoers rule scoped to the Borg binary restricts *who* may escalate, not
  *what* they may do: `--rsh` is a regular Borg option in both 1.4 and 2, so
  `sudo borg --rsh '<command>' …` executes arbitrary commands as root. It must
  be documented as granting the agent account effective root, not as
  confinement.
- `sudo` resets the environment, so `BORG_PASSPHRASE` would not reach the
  escalated process. `BORG_PASSCOMMAND` is preferable to an `env_keep`
  exception, since it keeps the secret out of the process list.

Forwarders and the `/opt` tree must be `root:root` and not writable by the
service user; otherwise the agent can rewrite a forwarder and any sudoers rule
becomes moot.

## Credential Model

The service user holds the SSH key for the remote repository and knows the
repository URL. It never holds `BORG_PASSPHRASE`: the passphrase arrives from
Borg UI per job over the agent session and is passed to the Borg child process
in its environment only. It is not at rest on the node.

Unprivileged users on the machine cannot reach any of it: `/etc/borg-ui-agent`
is `0750` and `config.toml` is `0600`.

## Scope

First implementation — backup only:

- server reports the Borg versions it runs, determined at runtime rather than
  maintained as a second constant;
- installer obtains those exact versions as static binaries, verified against
  a checksum manifest kept in this repository;
- installer obtains the agent from the enrolling server instead of GitHub;
- node layout and forwarders as above;
- `CAP_DAC_READ_SEARCH` on the unit;
- explicit failure with a distro fallback where no static binary exists.

Deliberately later:

- air-gapped operation. The node still reaches PyPI for the agent's two
  dependencies, and the binaries still come from the upstream release URL. The
  checksum manifest is the seed for that step: serving assets from the server
  changes only the base URL, not the shape of the script.
- restore with elevation.

Not in scope: replacing an existing Borg binary, automatic upgrades, macOS or
Windows.

## Open Points

- Whether `install.sh` moves from the Python string constant into a real file.
  Worth doing for maintainability, but it is the largest possible structural
  diff against upstream and would make every future upstream change to the
  installer a manual reconciliation for a fork. `shellcheck` coverage does not
  depend on it — the existing `bash -n` test already validates the served
  content and can call `shellcheck` instead. Best pursued as a separate,
  self-contained upstream change.

Resolved during implementation: whether the forwarders should shadow a
distribution `borg` by default. They must — see Node Layout above.
