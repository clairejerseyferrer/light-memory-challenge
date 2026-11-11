"""Minimal in-memory stand-in for the supabase-py query builder (no network)."""
from __future__ import annotations
import copy


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.payload = "select", None
        self.filters, self._order, self._limit = [], None, None

    def select(self, _cols="*"): self.op = "select"; return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def update(self, row): self.op, self.payload = "update", row; return self
    def eq(self, col, val): self.filters.append((col, "eq", val)); return self
    def lt(self, col, val): self.filters.append((col, "lt", val)); return self
    def order(self, col, desc=False): self._order = (col, desc); return self
    def limit(self, n): self._limit = n; return self

    def _match(self, row):
        for col, op, val in self.filters:
            if op == "eq" and row.get(col) != val: return False
            if op == "lt" and not (row.get(col) is not None and row[col] < val): return False
        return True

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.db.fail_table == self.table:
            raise RuntimeError("storage down")
        if self.op == "insert":
            if self.table == "game_adaptive_events":
                for r in rows:
                    if r["session_id"] == self.payload["session_id"] and r["completed_level"] == self.payload["completed_level"]:
                        raise RuntimeError("duplicate key value violates unique constraint")
            rows.append(copy.deepcopy(self.payload))
            return _Result([copy.deepcopy(self.payload)])
        matched = [r for r in rows if self._match(r)]
        if self.op == "update":
            for r in matched: r.update(copy.deepcopy(self.payload))
            return _Result(copy.deepcopy(matched))
        if self._order:
            matched.sort(key=lambda r: r[self._order[0]], reverse=self._order[1])
        if self._limit is not None: matched = matched[: self._limit]
        return _Result(copy.deepcopy(matched))


class FakeSupabase:
    def __init__(self):
        self.tables, self.fail_table = {}, None

    def table(self, name): return _Query(self, name)

    def add_session(self, sid="11111111-1111-1111-1111-111111111111", device="lmc-1", status="playing"):
        self.tables.setdefault("game_sessions", []).append({"id": sid, "device_id": device, "status": status})
        return sid
