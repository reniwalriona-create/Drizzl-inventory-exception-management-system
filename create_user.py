"""
Creates (or resets the password of) an application login -- the
supported, documented way to provision the initial user, since there is
deliberately no self-registration UI (Phase 12: minimal auth for an
internal tool, not a user-management product).

Usage:
    ./.venv/bin/python create_user.py <username>

Prompts for the password interactively (getpass, never echoed, never
taken as a command-line argument -- a password passed as an argv value
would sit in plain text in shell history and `ps` output). Hashes it
with Werkzeug's password hashing before storing -- the plaintext value
never touches the database or a log line.

Re-running with an existing username resets that user's password (after
a y/N confirmation) rather than erroring, so this same command is also
the documented way to recover a forgotten password.
"""
import getpass
import sys

from werkzeug.security import generate_password_hash

from db import get_connection


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    username = sys.argv[1].strip()
    if not username:
        print("Username can't be empty.")
        sys.exit(1)

    conn = get_connection()
    try:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing is not None:
            answer = input(f"User {username!r} already exists -- reset their password? [y/N] ").strip().lower()
            if answer != "y":
                print("Cancelled.")
                return

        password = getpass.getpass("New password: ")
        if len(password) < 8:
            print("Password must be at least 8 characters.")
            sys.exit(1)
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords did not match.")
            sys.exit(1)

        password_hash = generate_password_hash(password)
        if existing is not None:
            conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username))
            print(f"Password reset for {username!r}.")
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, password_hash)
            )
            print(f"User {username!r} created.")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
