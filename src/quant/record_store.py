"""User-owned immutable research/import records and their write audit trail."""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from quant.database import database_connection, initialize_database, LOCAL_ADMIN_USER_ID


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class RecordRepository:
    def __init__(self, path: Path, user_id=LOCAL_ADMIN_USER_ID):
        self.path, self.user_id = path, user_id

    def put(self, kind: str, key: str, payload: dict, *, after_insert=None):
        encoded = canonical(payload)
        if not key or len(key) > 200 or len(encoded.encode()) > 8*1024*1024:
            raise ValueError("Invalid import identity or record exceeds 8 MiB")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        initialize_database(self.path)
        with database_connection(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM records WHERE user_id=? AND kind=? AND import_key=?",
                (self.user_id, kind, key)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise ValueError("Import identity already exists with different content")
                return self._decode(row)
            identifier, now = str(uuid.uuid4()), utc_now()
            connection.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (identifier, self.user_id, kind, key, digest, now, encoded))
            connection.execute("INSERT INTO audit_events(user_id,operation,record_id,created_at) VALUES (?,?,?,?)",
                               (self.user_id, f"create:{kind}", identifier, now))
            if after_insert is not None:
                after_insert(connection)
            row = connection.execute("SELECT * FROM records WHERE id=?", (identifier,)).fetchone()
            return self._decode(row)

    def get(self, kind: str, identifier: str):
        with database_connection(self.path, read_only=True) as connection:
            row = connection.execute("SELECT * FROM records WHERE user_id=? AND kind=? AND id=?",
                                     (self.user_id, kind, identifier)).fetchone()
        if row is None:
            raise ValueError(f"Unknown {kind} record")
        return self._decode(row)

    def by_key(self, kind, key):
        with database_connection(self.path, read_only=True) as connection:
            row = connection.execute("SELECT * FROM records WHERE user_id=? AND kind=? AND import_key=?",
                                     (self.user_id, kind, key)).fetchone()
        return self._decode(row) if row else None

    def list(self, kind: str, limit=100, offset=0):
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("Invalid page")
        with database_connection(self.path, read_only=True) as connection:
            cursor = connection.execute("SELECT * FROM records WHERE user_id=? AND kind=? ORDER BY created_at,id LIMIT ? OFFSET ?",
                                        (self.user_id, kind, limit, offset))
            rows, size = [], 0
            for row in cursor:
                size += len(row["payload"].encode())
                if size > 8*1024*1024:
                    raise ValueError("Record page exceeds 8 MiB; request a smaller limit")
                rows.append(row)
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row):
        return {"id": row["id"], "kind": row["kind"], "importKey": row["import_key"],
                "digest": row["digest"], "createdAt": row["created_at"],
                "payload": json.loads(row["payload"])}
