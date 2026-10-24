"""analyst-inbox-mirror entry point (workflow .github/workflows/analyst-inbox-mirror.yml; register R-AN-83, §16.18 A6).

It keeps the agent inbox's main (PINS.inbox_repo) equal to gold-analyst main. With H = this checkout's HEAD (main) and
I = inbox main:
- I = H: nothing to do (`equal`);
- H is an ancestor of I: a newer run already pushed, nothing to do (`inbox_ahead`);
- I is an ancestor of H: push H to inbox main as a plain fast-forward push (`fast_forward`). The inbox deploy key
  bypasses the inbox's main ruleset, non_fast_forward included, so the fast-forward rule is this job's own;
- otherwise inbox main has diverged: the run fails (`inbox_diverged`) and pushes nothing.

The deploy key (environment secret INBOX_MIRROR_KEY of `analyst-inbox`, main only) is written to a 0600 file under
RUNNER_TEMP, used through GIT_SSH_COMMAND with IdentitiesOnly and strict host-key checking against GitHub's published
SSH host keys (https://api.github.com/meta), never printed, and removed at the end. It prints only shas and enums.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa")
KEY_RE = re.compile(r"(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]+")


def guard(env):
    """main only; a workflow_run must come from a run on main (R-AN-83)."""
    if env.get("GITHUB_REF") != "refs/heads/main":
        return False
    return env.get("GITHUB_EVENT_NAME") != "workflow_run" or env.get("WORKFLOW_RUN_BRANCH") == "main"


def _git(repo_dir, *args, env=None, runner=subprocess.run):
    e = dict(os.environ)
    e.update(env or {})
    e["GIT_TERMINAL_PROMPT"] = "0"
    return runner(["git", "-C", str(repo_dir), *args], capture_output=True, env=e)


def mirror(repo_dir, remote, env=None, runner=subprocess.run, log=print):
    """Fast-forward `remote`'s main to the HEAD of `repo_dir` when it is behind; 0 done, 1 failed."""
    h = _git(repo_dir, "rev-parse", "HEAD", runner=runner).stdout.decode().strip()
    f = _git(repo_dir, "fetch", "--quiet", "--no-tags", remote, "+refs/heads/main:refs/inbox/main", env=env,
             runner=runner)
    if f.returncode != 0:
        log(f"head {h} inbox_unreadable")
        return 1
    i = _git(repo_dir, "rev-parse", "refs/inbox/main", runner=runner).stdout.decode().strip()

    def ancestor(a, b):
        return _git(repo_dir, "merge-base", "--is-ancestor", a, b, runner=runner).returncode == 0

    if i == h:
        log(f"head {h} inbox {i} equal")
        return 0
    if ancestor(h, i):
        log(f"head {h} inbox {i} inbox_ahead")
        return 0
    if not ancestor(i, h):
        log(f"head {h} inbox {i} inbox_diverged")
        return 1
    p = _git(repo_dir, "push", "--quiet", remote, f"{h}:refs/heads/main", env=env, runner=runner)
    if p.returncode != 0:
        log(f"head {h} inbox {i} fast_forward push_refused")
        return 1
    log(f"head {h} inbox {i} fast_forward pushed")
    return 0


def known_hosts(meta):
    """known_hosts lines for github.com from the `ssh_keys` of GitHub's meta API."""
    keys = meta.get("ssh_keys") if isinstance(meta, dict) else None
    if not keys:
        raise ValueError("no published ssh keys")
    out = []
    for k in keys:
        if not isinstance(k, str) or not KEY_RE.fullmatch(k):
            raise ValueError("unexpected ssh key entry")
        out.append(f"github.com {k}\n")
    return "".join(out)


def write_key(directory, key):
    """The private key in a new 0600 file (OpenSSH needs the final newline); returns its path."""
    fd, path = tempfile.mkstemp(prefix="inbox_mirror_", dir=str(directory))
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
        f.write(key.replace("\r\n", "\n").rstrip("\n") + "\n")
    return path


def remove_key(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def main():
    env = dict(os.environ)
    if not guard(env):
        print("guard refused")
        return 1
    inbox = json.loads((ROOT / "analyst" / "PINS.json").read_text("utf-8")).get("inbox_repo")
    key = env.get("INBOX_MIRROR_KEY", "")
    if not inbox or not key:
        print("inbox_or_key_missing")
        return 1
    tmp = env.get("RUNNER_TEMP") or tempfile.gettempdir()
    with urllib.request.urlopen("https://api.github.com/meta", timeout=30) as r:
        hosts = known_hosts(json.loads(r.read().decode("utf-8")))
    kh = Path(tmp) / "github_known_hosts"
    kh.write_text(hosts, encoding="ascii")
    path = write_key(tmp, key)
    try:
        ssh = (f"ssh -i {shlex.quote(path)} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes "
               f"-o UserKnownHostsFile={shlex.quote(str(kh))}")
        return mirror(ROOT, f"git@github.com:{inbox}.git", env={"GIT_SSH_COMMAND": ssh})
    finally:
        remove_key(path)


if __name__ == "__main__":
    sys.exit(main())
