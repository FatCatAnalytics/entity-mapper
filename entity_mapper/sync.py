"""Bring the local workspace in line with the shared folder (decisions and master mapping)."""
from __future__ import annotations

from .shared import SharedFolder, current_user, decision_id, merge_decisions


def push_own(ws, shared: SharedFolder) -> int:
    """Append this user's local decisions that are not yet in their shared log. Returns how many."""
    user = current_user()
    known = {r.get("id") for r in shared.read_decisions(user)}
    mine = ws.alias_records(user)
    for r in mine:
        r["country"] = r.get("country") or ""
        r["id"] = decision_id(r)
    return shared.append_decisions([r for r in mine if r["id"] not in known], user)


def sync_shared(ws, shared: SharedFolder) -> dict:
    """Push own decisions, merge everyone's, and take the shared master mapping.

    Returns counts and the conflicts for the owner. If the folder cannot be written, the user's
    decisions stay local (and still take part in the merge) until a later sync succeeds.
    """
    out = {"writable": True, "pushed": 0, "push_error": None}
    try:
        out["pushed"] = push_own(ws, shared)
    except OSError as e:
        out["writable"], out["push_error"] = False, str(e)

    user = current_user()
    records = shared.read_decisions()
    known = {r.get("id") for r in records}
    for r in ws.alias_records(user):  # anything that could not be pushed still counts locally
        r["country"] = r.get("country") or ""
        r["id"] = decision_id(r)
        if r["id"] not in known:
            records.append(r)
    for r in records:
        r["country"] = r.get("country") or ""
    owner = shared.owner
    winners, conflicts = merge_decisions(records, owner)
    ws.replace_aliases(winners)
    out.update({"decisions": len(winners), "users": len({r.get("decided_by") for r in records}),
                "conflicts": conflicts, "owner": owner, "is_owner": bool(owner) and owner == user})

    # Master mapping: the shared file is the truth. A first-time user with local imports seeds it.
    if shared.master_path.exists():
        out["master_rows"] = ws.replace_master(shared.read_master())
    else:
        local = ws.master_rows()
        if len(local) and out["writable"]:
            local["imported_at"] = local["imported_at"].astype(str)
            try:
                shared.write_master(local, replace=True)
            except OSError as e:
                out["push_error"] = str(e)
        out["master_rows"] = len(local)
    return out
