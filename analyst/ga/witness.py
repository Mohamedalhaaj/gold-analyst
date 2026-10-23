"""Reading the script-written records on main: witnesses and terminals (register R-AN-35, R-AN-36). FROZEN."""
from . import paths


def witnesses(view, mode):
    out = []
    for p in view.list(f"{paths.prefix(mode)}witness/"):
        if p.endswith(".json"):
            doc = view.read_json(p)
            if isinstance(doc, dict):
                out.append(doc)
    out.sort(key=lambda d: d.get("seq", 0))
    return out


def accepted_attempts(view, mode, draw_id, ws=None):
    """The accepted attempt witnesses of a draw, by n."""
    ws = witnesses(view, mode) if ws is None else ws
    out = [w for w in ws if w.get("kind") == "attempt" and w.get("status") == "accepted" and w.get("id") == draw_id]
    return sorted(out, key=lambda w: int(w["path"].rsplit("_", 1)[1].split(".")[0]))


def attempt_n_of(w):
    return int(w["path"].rsplit("_", 1)[1].split(".")[0])


def terminal(view, mode, call_id):
    return view.read_json(paths.terminal_path(mode, call_id))


def has_terminal(view, mode, call_id):
    return view.exists(paths.terminal_path(mode, call_id))
