"""Read-only views of a repository tree (a git commit, a directory, or a dict for tests). FROZEN.

The session and the workflows read every input at a fixed commit (repo_head_sha) through GitView, so files in the
working tree that the agent may have edited are never read as inputs.
"""
import subprocess
from pathlib import Path


class View:
    def read(self, path):            # bytes or None
        raise NotImplementedError

    def list(self, prefix):          # sorted paths under prefix (files only)
        raise NotImplementedError

    def exists(self, path):
        return self.read(path) is not None

    def read_text(self, path):
        b = self.read(path)
        return None if b is None else b.decode("utf-8")

    def read_json(self, path):
        import json
        b = self.read(path)
        return None if b is None else json.loads(b.decode("utf-8"))


class DictView(View):
    def __init__(self, files):
        self.files = {k: (v.encode("utf-8") if isinstance(v, str) else v) for k, v in files.items()}

    def read(self, path):
        return self.files.get(path)

    def list(self, prefix):
        return sorted(p for p in self.files if p.startswith(prefix))


class DirView(View):
    def __init__(self, root):
        self.root = Path(root)

    def read(self, path):
        p = self.root / path
        return p.read_bytes() if p.is_file() else None

    def list(self, prefix):
        base = self.root / prefix
        d = base if base.is_dir() else base.parent
        if not d.exists():
            return []
        out = []
        for p in d.rglob("*"):
            if p.is_file():
                rel = p.relative_to(self.root).as_posix()
                if rel.startswith(prefix) and "/.git/" not in f"/{rel}":
                    out.append(rel)
        return sorted(out)


class GitView(View):
    """The tree of `rev` in the repository at `repo_dir`."""

    def __init__(self, repo_dir, rev):
        self.repo_dir = str(repo_dir)
        self.rev = self._git("rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()
        listing = self._git("ls-tree", "-r", "-z", self.rev)
        self._blobs = {}
        for ent in listing.split(b"\0"):
            if not ent:
                continue
            meta, path = ent.split(b"\t", 1)
            _mode, typ, sha = meta.split(b" ")
            if typ == b"blob":
                self._blobs[path.decode("utf-8")] = sha.decode()
        self._cache = {}

    def _git(self, *args):
        return subprocess.run(["git", "-C", self.repo_dir, *args], check=True, capture_output=True).stdout

    def read(self, path):
        sha = self._blobs.get(path)
        if sha is None:
            return None
        if sha not in self._cache:
            self._cache[sha] = self._git("cat-file", "blob", sha)
        return self._cache[sha]

    def list(self, prefix):
        return sorted(p for p in self._blobs if p.startswith(prefix))
