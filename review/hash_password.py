#!/usr/bin/env python3
"""生成审核面板登录密码哈希(PBKDF2-SHA256, 与主面板同格式)。

用法:
  python review/hash_password.py            # 交互输入
  python review/hash_password.py 密码文字   # 命令行传入

把输出整行粘进 review/config.yaml 的 users[].password_hash。
"""

from __future__ import annotations

import getpass
import hashlib
import secrets
import sys

from review.config import PBKDF2_ITERATIONS


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def main() -> None:
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        password = getpass.getpass("输入密码: ")
        confirm = getpass.getpass("再输一次: ")
        if password != confirm:
            print("两次输入不一致")
            sys.exit(1)
    if not password:
        print("密码不能为空")
        sys.exit(1)
    print(hash_password(password))


if __name__ == "__main__":
    main()
