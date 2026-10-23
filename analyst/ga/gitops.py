"""Git plumbing for the session scripts (register R-AN-30, R-AN-31). FROZEN.

Each push is a single new commit, holding exactly one added file, on a new branch based on the session's
origin/main. The commit is built with a temporary index (the working tree and the agent's own files are never
committed), its message carries only enums and ids, and a retried push sends the same commit to the same branch.
"""
import os
import subprocess
import tempfile

AUTHOR = ("gold-analyst session", "gold-analyst-session@users.noreply.github.com")


class GitError(Exception):
    pass


class Git:
    def __init__(self, repo_dir, remote="origin", runner=subprocess.run):
        self.repo_dir = str(repo_dir)
        self.remote = remote
        self.run = runner

    def _git(self, *args, env=None, input_bytes=None, check=True):
        e = dict(os.environ)
        if env:
            e.update(env)
        p = self.run(["git", "-C", self.repo_dir, *args], capture_output=True, env=e, input=input_bytes)
        if check and p.returncode != 0:
            raise GitError(f"git {args[0]} failed ({p.returncode})")
        return p

    def fetch_main(self):
        """Fetch origin main; returns origin/main's sha."""
        self._git("fetch", "--quiet", self.remote, f"+refs/heads/main:refs/remotes/{self.remote}/main")
        return self._git("rev-parse", f"{self.remote}/main").stdout.decode().strip()

    def commit_one_file(self, base_sha, path, data, message, when_epoch=None):
        blob = self._git("hash-object", "-w", "--stdin", input_bytes=data).stdout.decode().strip()
        with tempfile.TemporaryDirectory() as td:
            idx = os.path.join(td, "index")
            env = {"GIT_INDEX_FILE": idx}
            self._git("read-tree", base_sha, env=env)
            self._git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
            tree = self._git("write-tree", env=env).stdout.decode().strip()
        cenv = {"GIT_AUTHOR_NAME": AUTHOR[0], "GIT_AUTHOR_EMAIL": AUTHOR[1], "GIT_COMMITTER_NAME": AUTHOR[0],
                "GIT_COMMITTER_EMAIL": AUTHOR[1]}
        if when_epoch is not None:
            cenv["GIT_AUTHOR_DATE"] = cenv["GIT_COMMITTER_DATE"] = f"{int(when_epoch)} +0000"
        return self._git("commit-tree", tree, "-p", base_sha, "-m", message, env=cenv).stdout.decode().strip()

    def push(self, commit, branch, force=False):
        """(ok, short reason). Never raises on a refused push."""
        args = ["push", "--quiet", "--porcelain"]
        if force:
            args.append("--force")
        p = self._git(*args, self.remote, f"{commit}:refs/heads/{branch}", check=False)
        return p.returncode == 0, ("ok" if p.returncode == 0 else "refused")

    def tree_of(self, rev):
        return self._git("rev-parse", f"{rev}^{{tree}}").stdout.decode().strip()

    def first_parent(self, rev):
        return self._git("rev-parse", f"{rev}^").stdout.decode().strip()

    def commit_tree(self, tree, parents, message):
        cenv = {"GIT_AUTHOR_NAME": AUTHOR[0], "GIT_AUTHOR_EMAIL": AUTHOR[1], "GIT_COMMITTER_NAME": AUTHOR[0],
                "GIT_COMMITTER_EMAIL": AUTHOR[1]}
        args = ["commit-tree", tree]
        for p in parents:
            args += ["-p", p]
        return self._git(*args, "-m", message, env=cenv).stdout.decode().strip()

    def ls_remote_ok(self, url):
        p = self._git("ls-remote", "--heads", url, check=False)
        return p.returncode == 0
