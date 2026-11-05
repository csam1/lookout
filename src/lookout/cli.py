"""Admin CLI for internal API keys.

    python -m lookout.cli init-db
    python -m lookout.cli create-key --name billing-agent [--limit 10000] [--admin]
    python -m lookout.cli list-keys
    python -m lookout.cli revoke-key --id 3
    python -m lookout.cli usage
"""

import argparse
import asyncio

from sqlalchemy import func, select

from lookout.config import Settings
from lookout.db import ApiKey, Database, UsageEvent, month_start
from lookout.keys import create_key


async def run(args: argparse.Namespace) -> None:
    settings = Settings()
    db = Database(settings.database_url)
    try:
        await db.create_schema()
        async with db.sessions() as session:
            if args.command == "init-db":
                print("Schema ready.")

            elif args.command == "create-key":
                limit = args.limit or settings.default_monthly_credit_limit
                plaintext, row = await create_key(session, args.name, limit, is_admin=args.admin)
                role = "admin" if args.admin else "agent"
                print(f"Created {role} key id={row.id} name={row.name} monthly_limit={limit}")
                print(f"API key (shown once, store it now): {plaintext}")

            elif args.command == "list-keys":
                rows = (await session.execute(select(ApiKey).order_by(ApiKey.id))).scalars()
                for r in rows:
                    state = "active" if r.active else "revoked"
                    role = "admin" if r.is_admin else "agent"
                    print(f"{r.id:>4}  {r.key_prefix}…  {state:<8} {role:<6} limit={r.monthly_credit_limit:<8} {r.name}")

            elif args.command == "revoke-key":
                row = await session.get(ApiKey, args.id)
                if row is None:
                    raise SystemExit(f"No key with id {args.id}")
                row.active = False
                await session.commit()
                print(f"Revoked key id={row.id} ({row.name})")

            elif args.command == "usage":
                stmt = (
                    select(
                        ApiKey.id,
                        ApiKey.name,
                        func.count(UsageEvent.id),
                        func.coalesce(func.sum(UsageEvent.credits), 0),
                        ApiKey.monthly_credit_limit,
                    )
                    .join(
                        UsageEvent,
                        (UsageEvent.api_key_id == ApiKey.id) & (UsageEvent.created_at >= month_start()),
                        isouter=True,
                    )
                    .group_by(ApiKey.id)
                    .order_by(ApiKey.id)
                )
                print("This month (UTC):")
                for key_id, name, calls, credits, limit in await session.execute(stmt):
                    print(f"{key_id:>4}  {name:<30} calls={calls:<7} credits={credits}/{limit}")
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="lookout")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    create = sub.add_parser("create-key")
    create.add_argument("--name", required=True)
    create.add_argument("--limit", type=int, default=None, help="monthly credit limit")
    create.add_argument("--admin", action="store_true", help="admin key: can use /admin and /v1/admin")
    sub.add_parser("list-keys")
    revoke = sub.add_parser("revoke-key")
    revoke.add_argument("--id", type=int, required=True)
    sub.add_parser("usage")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
