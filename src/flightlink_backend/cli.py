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
    """通过终端交互创建管理员；密码不会通过命令行参数传入。"""
    try:
        username = input("管理员用户名：").strip()
    except (EOFError, KeyboardInterrupt):
        print("已取消创建。", file=sys.stderr)
        return 1

    if not USERNAME_PATTERN.fullmatch(username):
        print("用户名须为 3–64 个字符，只能使用英文字母、数字、点、下划线或连字符。", file=sys.stderr)
        return 2

    try:
        password = getpass.getpass("密码（至少 8 个字符）：")
        confirmation = getpass.getpass("确认密码：")
    except (EOFError, KeyboardInterrupt):
        print("已取消创建。", file=sys.stderr)
        return 1

    if len(password) < MIN_PASSWORD_LENGTH:
        print("密码至少需要 8 个字符。", file=sys.stderr)
        return 2
    if len(password) > MAX_PASSWORD_LENGTH:
        print("密码不能超过 1024 个字符。", file=sys.stderr)
        return 2
    if password != confirmation:
        print("两次输入的密码不一致。", file=sys.stderr)
        return 2

    initialize_database()
    connection = connect()
    try:
        connection.execute(
            "INSERT INTO admin_users(username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), int(time.time())),
        )
    except sqlite3.IntegrityError:
        print(f"管理员“{username}”已存在。", file=sys.stderr)
        return 3
    finally:
        connection.close()

    print(f"管理员“{username}”已创建。")
    return 0
