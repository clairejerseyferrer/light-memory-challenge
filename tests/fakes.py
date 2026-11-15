"""In-memory stand-in for the supabase-py client plus failure injection.

The fake exception classes deliberately share the names of httpx's / postgrest's
real ones; backend.reliability recognises errors by class name.
"""
import copy
import re
from itertools import count


class ConnectError(Exception): pass
class ConnectTimeout(Exception): pass
class ReadTimeout(Exception): pass
class RemoteProtocolError(Exception): pass


class APIError(Exception):
    def __init__(self, code, message="db error"):
        super().__init__({"code": code, "message": message})
        self.code, self.message = code, message


class Resp:
    def __init__(self, data):
        self.data = data


def _like_regex(pattern):
    out, i = "", 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out += re.escape(pattern[i + 1]); i += 2; continue
        out += ".*" if ch == "%" else "." if ch == "_" else re.escape(ch)
        i += 1
    return re.compile("^" + out + "$", re.I | re.S)


class FakeDB:
    def __init__(self):
        self.tables = {"game_sessions": [], "game_results": []}
        self.calls = []        # (table, op) for every execute attempt
        self.hooks = []        # callables (table, op, attempt_no) -> may raise / return "after"
        self._seq = count(1)
        self._attempt = 0

    def client(self):
        return FakeClient(self)

    def add_session(self, sid, codename, status="completed", device="ESP32-001", completed_at=None):
        row = {"id": sid, "session_number": next(self._seq), "codename": codename, "device_id": device,
               "status": status, "created_at": "2026-11-01T10:00:00+00:00", "start_at": None,
               "started_at": None, "completed_at": completed_at}
        self.tables["game_sessions"].append(row)
        return row

    def add_result(self, sid, score, level, saved_at, accuracy=80.0, **extra):
        row = {"session_id": sid, "score": score, "highest_level": level, "accuracy": accuracy,
               "saved_at": saved_at, "mistakes": 0, "total_rounds": 5, "round_data": [], "analysis": {}}
        row.update(extra)
        self.tables["game_results"].append(row)
        return row


class FakeClient:
    def __init__(self, db):
        self.db = db

    def table(self, name):
        return FakeQuery(self.db, name)


class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.payload, self.cols = "select", None, "*"
        self.filters, self.orders, self._limit, self._range = [], [], None, None

    def select(self, cols="*"): self.op, self.cols = "select", cols; return self
    def insert(self, payload): self.op, self.payload = "insert", payload; return self
    def update(self, payload): self.op, self.payload = "update", payload; return self
    def delete(self): self.op = "delete"; return self
    def eq(self, c, v): self.filters.append(("eq", c, v)); return self
    def neq(self, c, v): self.filters.append(("neq", c, v)); return self
    def in_(self, c, v): self.filters.append(("in", c, list(v))); return self
    def ilike(self, c, v): self.filters.append(("ilike", c, v)); return self
    def order(self, c, desc=False): self.orders.append((c, desc)); return self
    def limit(self, n): self._limit = n; return self
    def range(self, a, b): self._range = (a, b); return self

    def _match(self, row):
        for kind, c, v in self.filters:
            x = row.get(c)
            if kind == "eq" and x != v: return False
            if kind == "neq" and x == v: return False
            if kind == "in" and x not in v: return False
            if kind == "ilike" and not _like_regex(v).match(str(x or "")): return False
        return True

    def execute(self):
        db = self.db
        db.calls.append((self.table, self.op))
        db._attempt += 1
        after = None
        for hook in db.hooks:
            outcome = hook(self.table, self.op, db._attempt)
            if outcome == "after":
                after = hook
        rows = db.tables[self.table]
        if self.op == "insert":
            pk = "id" if self.table == "game_sessions" else "session_id"
            row = dict(self.payload)
            if any(r[pk] == row[pk] for r in rows):
                raise APIError("23505", "duplicate key value violates unique constraint")
            if self.table == "game_sessions":
                row.setdefault("session_number", next(db._seq))
                row.setdefault("created_at", "2026-11-14T00:00:00+00:00")
                row.setdefault("start_at", None); row.setdefault("started_at", None)
                row.setdefault("completed_at", None)
            rows.append(row)
            if after:  # the write landed but the response is lost
                raise ReadTimeout("response lost")
            return Resp([copy.deepcopy(row)])
        matched = [r for r in rows if self._match(r)]
        if self.op == "update":
            for r in matched: r.update(self.payload)
            return Resp(copy.deepcopy(matched))
        if self.op == "delete":
            for r in matched: rows.remove(r)
            return Resp(copy.deepcopy(matched))
        for c, desc in reversed(self.orders):
            matched.sort(key=lambda r: (r.get(c) is None, r.get(c)), reverse=desc)
        if self._range:
            matched = matched[self._range[0]:self._range[1] + 1]
        if self._limit is not None:
            matched = matched[: self._limit]
        if self.cols != "*":
            keep = [c.strip() for c in self.cols.split(",")]
            matched = [{k: r.get(k) for k in keep} for r in matched]
        return Resp(copy.deepcopy(matched))


def fail_times(table, op, exc_factory, times):
    """Hook: raise ``exc_factory()`` the first ``times`` matching calls."""
    state = {"n": 0}
    def hook(t, o, attempt):
        if t == table and o == op and state["n"] < times:
            state["n"] += 1
            raise exc_factory()
    hook.state = state
    return hook


def lose_response(table, op):
    """Hook: the first matching write is applied but its response is lost."""
    state = {"done": False}
    def hook(t, o, attempt):
        if t == table and o == op and not state["done"]:
            state["done"] = True
            return "after"
    return hook
