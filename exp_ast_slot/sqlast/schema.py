"""Optional relational-schema facts used to fill schema-dependent slots.

Only BIRD ships machine-readable schema files (``*_tables.json``) with column
types, primary keys and foreign keys; Spider2 gold SQL is schema-less here, so
every lookup degrades to ``None`` and the affected slots are reported as
``unknown`` instead of being guessed.

Schema file format (BIRD): a JSON list, one entry per database::

    {"db_id": "...",
     "table_names_original": [...], "column_names_original": [[tid, "col"], ...],
     "column_types": [...], "primary_keys": [col_idx, ...],
     "foreign_keys": [[col_idx, col_idx], ...]}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

__all__ = ["Schema", "DBSchema", "load_schema"]


@dataclass
class DBSchema:
    db_id: str
    tables: list[str] = field(default_factory=list)
    columns: list[tuple[int, str]] = field(default_factory=list)
    column_types: list[str] = field(default_factory=list)
    primary_keys: list[int] = field(default_factory=list)
    foreign_keys: list[tuple[int, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_table: dict[str, dict[str, str]] = {}
        for idx, (tid, name) in enumerate(self.columns):
            if tid < 0:
                continue
            tname = self.tables[tid]
            self._by_table.setdefault(_norm(tname), {})[_norm(name)] = \
                self.column_types[idx] if idx < len(self.column_types) else "text"
        self._pk = {_named_key(self, self.columns[i])
                    for i in _flat_ints(self.primary_keys)
                    if 0 <= i < len(self.columns)}
        self._fk = set()
        self._fk_pairs: list[tuple[str, str, str, str]] = []
        for pair in self.foreign_keys:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            a, b = pair
            if not (0 <= a < len(self.columns) and 0 <= b < len(self.columns)):
                continue
            ta, ca = self.columns[a]
            tb, cb = self.columns[b]
            if ta < 0 or tb < 0:
                continue
            self._fk.add(_named_key(self, self.columns[a]))
            self._fk.add(_named_key(self, self.columns[b]))
            self._fk_pairs.append((_norm(self.tables[ta]), _norm(ca),
                                   _norm(self.tables[tb]), _norm(cb)))

    # -- lookups ------------------------------------------------------------
    def column_type(self, table: str | None, column: str) -> str | None:
        if not table:
            return None
        cols = self._by_table.get(_norm(table))
        if not cols:
            return None
        return cols.get(_norm(column))

    def is_primary_key(self, table: str | None, column: str) -> bool | None:
        if not table:
            return None
        return (_norm(table), _norm(column)) in self._pk

    def is_foreign_key(self, table: str | None, column: str) -> bool | None:
        if not table:
            return None
        return (_norm(table), _norm(column)) in self._fk

    def fk_between(self, t1: str | None, c1: str, t2: str | None, c2: str) -> bool:
        """True when ``t1.c1 = t2.c2`` is declared as a foreign-key relation."""
        if not (t1 and t2):
            return False
        a = (_norm(t1), _norm(c1))
        b = (_norm(t2), _norm(c2))
        for ft1, fc1, ft2, fc2 in self._fk_pairs:
            if (a == (ft1, fc1) and b == (ft2, fc2)) or (a == (ft2, fc2) and b == (ft1, fc1)):
                return True
        return False

    def ambiguous_pair(self, t1: str | None, t2: str | None) -> bool:
        """True when two tables are linked by more than one foreign key."""
        if not (t1 and t2):
            return False
        a, b = _norm(t1), _norm(t2)
        n = sum(1 for ft1, _, ft2, _ in self._fk_pairs
                if {ft1, ft2} == {a, b} and ft1 != ft2)
        return n > 1

    def tables_present(self) -> set[str]:
        return set(self._by_table)


class Schema:
    """Container for several databases loaded from one BIRD ``tables.json``."""

    def __init__(self) -> None:
        self._dbs: dict[str, DBSchema] = {}

    @classmethod
    def from_file(cls, path: str) -> "Schema":
        inst = cls()
        with open(path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
        for row in rows:
            db = DBSchema(
                db_id=row["db_id"],
                tables=row["table_names_original"],
                columns=[(t, c) for t, c in row["column_names_original"]],
                column_types=row["column_types"],
                primary_keys=row["primary_keys"],
                foreign_keys=row["foreign_keys"],
            )
            inst._dbs[row["db_id"]] = db
            inst._dbs[_norm(row["db_id"])] = db
        return inst

    def db(self, db_id: str | None) -> DBSchema | None:
        if not db_id:
            return None
        return self._dbs.get(db_id) or self._dbs.get(_norm(db_id))

    def __len__(self) -> int:
        return len({id(v) for v in self._dbs.values()})


def load_schema(path: str | None) -> Schema | None:
    if not path:
        return None
    return Schema.from_file(path)


def _norm(name: str) -> str:
    return name.strip().strip('`"').lower()


def _flat_ints(seq) -> list[int]:
    """BIRD writes primary keys either flat (``[3, 7]``) or as composite
    groups (``[[3], [7, 9]]``); normalise both to a flat index list."""
    out: list[int] = []
    for item in seq or []:
        if isinstance(item, (list, tuple)):
            out.extend(int(x) for x in item if isinstance(x, int))
        elif isinstance(item, int):
            out.append(item)
    return out


def _named_key(db: "DBSchema", pair: tuple[int, str]) -> tuple[str, str]:
    """Primary/foreign keys are indexed by (table name, column name)."""
    tid, col = pair
    return (_norm(db.tables[tid]), _norm(col))
