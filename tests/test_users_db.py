"""SQL for isAdmin and the optional user flags (the DB driver is mocked)."""
from __future__ import annotations

import app as app_module


def recording_connection(monkeypatch, existing):
    statements = []

    class Cursor:
        lastrowid = 9

        def execute(self, statement, parameters=None):
            statements.append((" ".join(statement.split()), parameters))

        def fetchone(self):
            return existing

        def fetchall(self):
            return []

        def close(self):
            pass

    class Connection:
        def cursor(self, dictionary=False):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            raise AssertionError("must not roll back")

        def close(self):
            pass

    monkeypatch.setattr(app_module.mysql.connector, "connect", lambda **kwargs: Connection())
    monkeypatch.setattr(app_module, "get_current_ist_time", lambda: "NOW")
    return statements


def test_fetch_users_selects_is_admin(monkeypatch):
    statements = recording_connection(monkeypatch, None)
    app_module.fetch_users()
    assert "isAdmin" in statements[0][0]


def test_update_writes_is_admin_and_skips_left_out_flags(monkeypatch):
    statements = recording_connection(monkeypatch, {"id": 4})
    app_module.upsert_user("u", "u@x.com", "U", True, True, False, False, False, False, False, None, None, True)
    statement, parameters = statements[1]
    assert "isMobile" not in statement and "isDeploy" not in statement
    assert "isDataMigration = %s, isAdmin = %s, updated_at = %s" in statement
    assert parameters == ("u", "u@x.com", "U", 1, 1, 0, 0, 0, 0, 0, 1, "NOW", 4)


def test_update_without_is_admin_leaves_it(monkeypatch):
    statements = recording_connection(monkeypatch, {"id": 4})
    app_module.upsert_user("u", "u@x.com", "U", True, True, False, False, False, False, False, True, False)
    assert "isAdmin" not in statements[1][0]


def test_insert_writes_is_admin_when_given(monkeypatch):
    statements = recording_connection(monkeypatch, None)
    app_module.upsert_user("u", "u@x.com", "U", True, False, False, False, False, False, False, None, None, False)
    statement, parameters = statements[1]
    assert "isDeploy, isAdmin, created_at, updated_at" in statement
    assert statement.count("%s") == len(parameters) == 15
    assert parameters[-5:] == (0, 0, 0, "NOW", "NOW")
