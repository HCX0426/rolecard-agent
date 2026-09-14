"""Bootstrap SQLite: apply schema in the fixed order (see storage/db.py), then seed the
built-in roles from roles/seed.py.

Built-in roles live in CODE, not in a SQL fixture: `medical_archivist` is undeletable
(role_card.is_builtin = 1) and its tool whitelist is a security boundary, so it must not be
ordinary editable data.
"""
