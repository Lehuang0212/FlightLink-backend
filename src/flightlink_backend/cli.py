from __future__ import annotations

import getpass
import os
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
    def text(zh: str, en: str) -> str:
        return en if os.getenv('FLIGHTLINK_CLI_LANGUAGE') == 'en' else zh

    cancelled = text('已取消创建。', 'Cancelled.')
    try:
        username = input(text('管理员用户名：', 'Administrator username: ')).strip()
    except (EOFError, KeyboardInterrupt):
        print(cancelled, file=sys.stderr)
        return 1

    if not USERNAME_PATTERN.fullmatch(username):
        print(text('用户名须为 3–64 个字符，只能使用英文字母、数字、点、下划线或连字符。', 'Use 3–64 letters, digits, dots, underscores or hyphens.'), file=sys.stderr)
        return 2

    try:
        password = getpass.getpass(text('密码（至少 8 个字符）：', 'Password (at least 8 characters): '))
        confirmation = getpass.getpass(text('确认密码：', 'Confirm password: '))
    except (EOFError, KeyboardInterrupt):
        print(cancelled, file=sys.stderr)
        return 1

    if len(password) < MIN_PASSWORD_LENGTH:
        print(text('密码至少需要 8 个字符。', 'Password must be at least 8 characters.'), file=sys.stderr)
        return 2
    if len(password) > MAX_PASSWORD_LENGTH:
        print(text('密码不能超过 1024 个字符。', 'Password cannot exceed 1024 characters.'), file=sys.stderr)
        return 2
    if password != confirmation:
        print(text('两次输入的密码不一致。', 'Passwords do not match.'), file=sys.stderr)
        return 2

    initialize_database()
    connection = connect()
    try:
        connection.execute(
            "INSERT INTO admin_users(username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), int(time.time())),
        )
    except sqlite3.IntegrityError:
        print(text(f'管理员“{username}”已存在。', f'Administrator {username} already exists.'), file=sys.stderr)
        return 3
    finally:
        connection.close()

    print(text(f'管理员“{username}”已创建。', f'Created administrator {username}.'))
    return 0
