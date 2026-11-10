#!/usr/bin/env python3
"""系统密码重置与管理工具 (Password Reset & Management CLI)

使用方式：
  交互式修改（推荐，输入不回显）：
    python3 set_password.py

  命令行直接指定（适合脚本或批处理）：
    python3 set_password.py --password "YourNewPassword123!"

特性：
- 严格遵循 600,000 轮 PBKDF2-HMAC-SHA256 哈希计算
- 自动生成 32 字节密码学随机盐
- 修改成功后自动注销所有已有在线会话，杜绝旧凭证滞留
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

# 确保能引入同目录的 auth 模块
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import auth


def main() -> None:
    parser = argparse.ArgumentParser(description="八字历史规律系统 · 密码重置工具")
    parser.add_argument("--password", "-p", type=str, default="", help="直接指定新密码（不推荐在公共终端使用）")
    args = parser.parse_args()

    print("=" * 60)
    print("🔒 八字历史规律本地查询系统 · 访问密码安全设置")
    print("=" * 60)

    new_pwd = args.password
    if not new_pwd:
        try:
            while True:
                new_pwd = getpass.getpass("请输入新的访问密码（至少6位）：").strip()
                if not new_pwd:
                    print("❌ 密码不能为空，请重新输入。")
                    continue
                if len(new_pwd) < 6:
                    print("❌ 密码安全强度不足：长度不能少于 6 位，建议包含大小写字母、数字或符号。")
                    continue
                confirm_pwd = getpass.getpass("请再次输入新密码以确认：").strip()
                if new_pwd != confirm_pwd:
                    print("❌ 两次输入的密码不一致，请重新输入。\n")
                    continue
                break
        except (KeyboardInterrupt, EOFError):
            print("\n操作已取消。")
            sys.exit(1)
    else:
        if len(new_pwd) < 6:
            print("❌ 密码长度不能少于 6 位。")
            sys.exit(1)

    try:
        auth.update_password(new_pwd)
        print("\n✅ 密码修改成功！")
        print("🔐 加密方案：PBKDF2-HMAC-SHA256 (600,000 轮安全哈希 + 32 字节独立随机盐)")
        print("⚡ 所有正在登录的旧设备会话已全部安全吊销，下次访问需使用新密码登录。")
        print("=" * 60)
    except Exception as exc:
        print(f"\n❌ 密码修改失败: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
