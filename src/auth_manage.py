"""CLI: управление пользователями (без Streamlit)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from auth import (  # noqa: E402
    PAGE_CATALOG,
    USER_ASSIGNABLE_PAGES,
    create_user,
    list_users_for_admin,
    set_user_active,
    set_user_pages,
    set_user_role,
    users_file_path,
)
from auth_sessions import revoke_all_user_sessions  # noqa: E402
from auth_store import auth_db_path, init_auth_db  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Пользователи Order Flow Analyzer")
    sub = p.add_subparsers(dest="cmd", required=True)

    add_p = sub.add_parser("add", help="Создать пользователя (без подтверждения email)")
    add_p.add_argument("username")
    add_p.add_argument("password")
    add_p.add_argument("--email", default="", help="Email (опционально)")
    add_p.add_argument(
        "--unverified",
        action="store_true",
        help="Email не подтверждён (для теста цепочки подтверждения)",
    )
    add_p.add_argument("--role", choices=("user", "admin"), default="user")
    add_p.add_argument(
        "--pages",
        default="",
        help=f"Страницы через запятую: {', '.join(USER_ASSIGNABLE_PAGES)}",
    )

    sub.add_parser("list", help="Список пользователей")

    ban_p = sub.add_parser("ban", help="Заблокировать пользователя")
    ban_p.add_argument("username")

    unban_p = sub.add_parser("unban", help="Разблокировать пользователя")
    unban_p.add_argument("username")

    role_p = sub.add_parser("set-role", help="Назначить роль admin или user")
    role_p.add_argument("username")
    role_p.add_argument("role", choices=("admin", "user"))

    pages_p = sub.add_parser("set-pages", help="Выдать доступ к страницам")
    pages_p.add_argument("username")
    pages_p.add_argument("pages", nargs="+", choices=USER_ASSIGNABLE_PAGES)

    clear_sess_p = sub.add_parser("clear-sessions", help="Завершить все сессии пользователя")
    clear_sess_p.add_argument("username")

    args = p.parse_args()
    path = users_file_path()
    print(f"Файл: {path}")
    print(f"Auth DB: {auth_db_path()}")

    if args.cmd == "add":
        try:
            email = str(args.email or "").strip() or None
            verified = not bool(args.unverified)
            pages = [s.strip() for s in str(args.pages or "").split(",") if s.strip()] or None
            create_user(
                args.username,
                args.password,
                email=email,
                email_verified=verified if email else True,
                role=args.role,
                pages=pages,
            )
            print(f"OK: {args.username.strip().lower()} ({args.role})")
        except ValueError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "list":
        for row in list_users_for_admin():
            pages = ", ".join(row.get("pages") or [])
            print(
                f"  {row['username']}  role={row.get('role', 'user')}  active={row['active']}"
                f"  email={row.get('email') or '—'}  verified={row.get('email_verified', True)}"
                f"  pages={pages}"
            )
    elif args.cmd == "ban":
        try:
            set_user_active(args.username, False)
            print(f"OK: {args.username.strip().lower()} заблокирован")
        except ValueError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "unban":
        try:
            set_user_active(args.username, True)
            print(f"OK: {args.username.strip().lower()} разблокирован")
        except ValueError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "set-role":
        try:
            set_user_role(args.username, args.role)
            print(f"OK: {args.username.strip().lower()} → {args.role}")
        except ValueError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "set-pages":
        try:
            set_user_pages(args.username, list(args.pages))
            labels = ", ".join(PAGE_CATALOG.get(p, p) for p in args.pages)
            print(f"OK: {args.username.strip().lower()} → {labels}")
        except ValueError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "clear-sessions":
        init_auth_db()
        name = args.username.strip().lower()
        n = revoke_all_user_sessions(name, reason="admin")
        print(f"OK: завершено сессий: {n}")


if __name__ == "__main__":
    main()
