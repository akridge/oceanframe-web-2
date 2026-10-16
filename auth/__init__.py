"""
Login, users and roles.

The design is CAT's (cat/db/auth.py, cat/api/auth.py), which has held up in the
field: bcrypt password hashes, an opaque random session token of which only the
SHA-256 is stored, an httponly cookie, and FastAPI dependencies for roles. It is
re-expressed on the SQLAlchemy layer so it runs on Oracle and SQLite alike.

``OCEANFRAME_AUTH=off`` (the default for a local run) keeps every page open and
treats each request as a built-in ``local`` admin, so the rest of the code has
one path: there is always a current user, and attribution always has someone to
point at.
"""
