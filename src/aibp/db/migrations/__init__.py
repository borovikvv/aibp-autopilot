"""Database migrations — lightweight, no external dependencies.

Each migration is a Python module in this directory named NNNN_description.py.
It must define:
    up(conn)  — apply migration
    down(conn) — rollback (optional, can raise NotImplementedError)

Migrations are tracked in `_migrations` table in PostgreSQL.
"""
