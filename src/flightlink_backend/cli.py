from __future__ import annotations

import getpass
import re
import sqlite3
import sys
import time

from .database import connect, initialize_database
from .security import hash_password

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024


def add_admin() -> int:
    """Create an administrator interactively; never accept a password as an argument."""
    try:
        username = input("Administrator username: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 1

    if not USERNAME_PATTERN.fullmatch(username):
        print("Username must be 3-64 characters: letters, numbers, dot, underscore or hyphen.", file=sys.stderr)
        return 2

    try:
        password = getpass.getpass("Password (at least 8 characters): ")
        confirmation = getpass.getpass("Confirm password: ")
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 1

    if len(password) < MIN_PASSWORD_LENGTH:
        print("Password must contain at least 8 characters.", file=sys.stderr)
        return 2
    if len(password) > MAX_PASSWORD_LENGTH:
        print("Password must contain no more than 1024 characters.", file=sys.stderr)
        return 2
    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 2

    initialize_database()
    connection = connect()
    try:
        connection.execute(
            "INSERT INTO admin_users(username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), int(time.time())),
        )
    except sqlite3.IntegrityError:
        print(f"Administrator '{username}' already exists.", file=sys.stderr)
        return 3
    finally:
        connection.close()

    print(f"Administrator '{username}' created.")
    return 0
