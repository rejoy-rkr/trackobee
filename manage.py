#!/usr/bin/env python3
"""Trackobee management.  Run with the venv:  .venv/bin/python manage.py <command>
  check                 test the database connection (Supabase/Postgres or local SQLite)
  create-owner EMAIL    create/replace the platform owner (prompts for password; or set TB_OWNER_PASSWORD)
"""
import getpass, os, sys, time
from werkzeug.security import generate_password_hash
import app as tb

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "check":
        with tb.db() as c: n = c.execute("SELECT COUNT(*) AS n FROM orgs").fetchone()["n"]
        print(f"database OK ({'Postgres/Supabase' if tb.PG else 'SQLite'}), {n} workspace(s)")
    elif cmd == "create-owner" and len(sys.argv) > 2:
        email = sys.argv[2].strip().lower()
        pw = os.environ.get("TB_OWNER_PASSWORD") or getpass.getpass("Owner password (10+ chars): ")
        if not tb.ok_password(pw): sys.exit("password must be at least 10 characters")
        with tb.db() as c:
            row = c.execute("SELECT id, role FROM users WHERE email=?", (email,)).fetchone()
            if row and row["role"] != "owner": sys.exit("that email already belongs to a workspace user")
            if row: c.execute("UPDATE users SET pw_hash=?, active=1 WHERE id=?", (generate_password_hash(pw), row["id"]))
            else: c.execute("INSERT INTO users(org_id,email,name,pw_hash,role,created) VALUES(NULL,?,?,?,?,?)",
                            (email, "Platform owner", generate_password_hash(pw), "owner", int(time.time())))
        print("owner ready:", email)
    else:
        print(__doc__); sys.exit(1)

if __name__ == "__main__":
    main()
