"""Read-only git access for the analyst judge. Pushed agent commits are only ever READ (cat-file, diff); nothing from
them is executed."""
from __future__ import annotations

import subprocess


class GitError(RuntimeError):
    pass


class GitRepo:
    def __init__(self, path: str):
        self.path = path
        self._batch = None
        self._cache = {}
        self._trees = {}

    # ---------------------------------------------------------------- plumbing
    def run(self, *args, check=True) -> str:
        p = subprocess.run(["git", "-c", "core.quotePath=false", "-C", self.path, *args],
                           capture_output=True, text=True)
        if check and p.returncode != 0:
            raise GitError(f"git {args[0]} failed: {p.stderr.strip()[:200]}")
        return p.stdout

    def ok(self, *args) -> bool:
        p = subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True)
        return p.returncode == 0

    def close(self):
        if self._batch is not None:
            try:
                self._batch.stdin.close()
                self._batch.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self._batch.kill()
            self._batch = None

    # ---------------------------------------------------------------- refs
    def head(self) -> str:
        return self.run("rev-parse", "HEAD").strip()

    def rev(self, ref: str):
        p = subprocess.run(["git", "-C", self.path, "rev-parse", "--verify", "-q", ref + "^{commit}"],
                           capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None

    def is_shallow(self) -> bool:
        return self.run("rev-parse", "--is-shallow-repository").strip() == "true"

    def commit_exists(self, sha: str) -> bool:
        if not isinstance(sha, str) or len(sha) != 40:
            return False
        return self.ok("cat-file", "-e", sha + "^{commit}")

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)

    def merge_base(self, a: str, b: str):
        p = subprocess.run(["git", "-C", self.path, "merge-base", a, b], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None

    # ---------------------------------------------------------------- blobs
    def show(self, commit: str, path: str):
        """Bytes of path at commit, or None if absent."""
        key = (commit, path)
        if key in self._cache:
            return self._cache[key]
        if self._batch is None:
            self._batch = subprocess.Popen(["git", "-C", self.path, "cat-file", "--batch"],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self._batch.stdin.write(f"{commit}:{path}\n".encode("utf-8"))
        self._batch.stdin.flush()
        header = self._batch.stdout.readline().decode("utf-8", "replace").rstrip("\n")
        parts = header.split(" ")
        if len(parts) != 3 or parts[1] != "blob":
            data = None
            if len(parts) == 3:  # a tree or other object: consume it
                self._batch.stdout.read(int(parts[2]) + 1)
        else:
            size = int(parts[2])
            data = self._batch.stdout.read(size)
            self._batch.stdout.read(1)
        if len(self._cache) > 20000:
            self._cache.clear()
        self._cache[key] = data
        return data

    def ls_tree(self, commit: str, prefix: str = ""):
        """Paths of the tree of `commit` under `prefix` (one git call per commit, cached: commits are immutable)."""
        sha = self.rev(commit) if len(commit) != 40 else commit
        if sha is None:
            return []
        tree = self._trees.get(sha)
        if tree is None:
            tree = [x for x in self.run("ls-tree", "-r", "--name-only", sha).split("\n") if x]
            if len(self._trees) > 200:
                self._trees.clear()
            self._trees[sha] = tree
        return [p for p in tree if p.startswith(prefix)] if prefix else list(tree)

    # ---------------------------------------------------------------- history
    def first_parent_log(self, ref: str = "HEAD"):
        """[(sha, parent_or_None, [(status, path)])] oldest first, main's first-parent history, no renames."""
        out = self.run("log", "--first-parent", "--diff-merges=first-parent", "--no-renames", "--name-status",
                       "--format=@@@%H %P", ref)
        commits = []
        cur = None
        for line in out.split("\n"):
            if line.startswith("@@@"):
                parts = line[3:].split(" ")
                cur = (parts[0], parts[1] if len(parts) > 1 and parts[1] else None, [])
                commits.append(cur)
            elif line.strip() and cur is not None:
                st, _, path = line.partition("\t")
                cur[2].append((st.strip(), path))
        commits.reverse()
        return commits

    def diff_name_status(self, base: str, after: str):
        out = self.run("diff", "--no-renames", "--name-status", base, after)
        res = []
        for line in out.split("\n"):
            if line.strip():
                st, _, path = line.partition("\t")
                res.append((st.strip(), path))
        return res

    def tag_commit(self, tag: str):
        return self.rev(f"refs/tags/{tag}")
