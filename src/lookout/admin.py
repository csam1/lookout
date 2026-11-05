"""Admin API: manage keys and read usage across all keys. Requires an admin key.

Revoking is permanent (a revoked key may have leaked). The last active admin key cannot be
revoked, so the service can't be locked out of its own admin API. Every change is written to
the lookout.audit log.
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lookout.db import ApiKey, as_utc, daily_usage, keys_with_usage, month_start, month_totals
from lookout.errors import ApiError
from lookout.keys import create_key
from lookout.models import (
    AdminKey, AdminUsage, CreatedKey, CreateKeyRequest, ErrorResponse, UpdateKeyRequest,
)

audit_log = logging.getLogger("lookout.audit")


def _audit(actor: ApiKey, action: str, target: ApiKey, **details) -> None:
    audit_log.info(json.dumps({
        "action": action,
        "actor_key_id": actor.id,
        "actor_key": actor.key_prefix,
        "target_key_id": target.id,
        **details,
    }))


def _to_admin_key(key: ApiKey, calls: int = 0, credits: int = 0, last_used: datetime | None = None) -> AdminKey:
    return AdminKey(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        is_admin=key.is_admin,
        active=key.active,
        monthly_credit_limit=key.monthly_credit_limit,
        calls_this_month=calls,
        credits_this_month=credits,
        last_used_at=as_utc(last_used),
        created_at=as_utc(key.created_at),
    )


def admin_router(
    get_session: Callable,
    current_key: Callable,
    default_monthly_credit_limit: int,
) -> APIRouter:
    async def require_admin(key: ApiKey = Depends(current_key)) -> ApiKey:
        if not key.is_admin:
            raise ApiError(403, "forbidden", "This key is not an admin key.")
        return key

    errors = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}}
    router = APIRouter(prefix="/v1/admin", tags=["admin"], responses=errors)

    async def load(session: AsyncSession, key_id: int) -> ApiKey:
        key = await session.get(ApiKey, key_id)
        if key is None:
            raise ApiError(404, "not_found", f"No key with id {key_id}.")
        return key

    async def with_usage(session: AsyncSession, key_id: int) -> AdminKey:
        for key, calls, credits, last_used in await keys_with_usage(session):
            if key.id == key_id:
                return _to_admin_key(key, calls, credits, last_used)
        raise ApiError(404, "not_found", f"No key with id {key_id}.")

    @router.get("/keys", response_model=list[AdminKey])
    async def list_keys(
        _: ApiKey = Depends(require_admin), session: AsyncSession = Depends(get_session)
    ) -> list[AdminKey]:
        return [_to_admin_key(*row) for row in await keys_with_usage(session)]

    @router.post("/keys", response_model=CreatedKey, status_code=201)
    async def create(
        body: CreateKeyRequest,
        actor: ApiKey = Depends(require_admin),
        session: AsyncSession = Depends(get_session),
    ) -> CreatedKey:
        name = body.name.strip()
        if not name:
            raise ApiError(422, "invalid_request", "name: must not be blank")
        limit = body.monthly_credit_limit or default_monthly_credit_limit
        plaintext, key = await create_key(session, name, limit, is_admin=body.is_admin)
        _audit(actor, "create_key", key, name=name, monthly_credit_limit=limit, is_admin=body.is_admin)
        return CreatedKey(key=_to_admin_key(key), api_key=plaintext)

    @router.patch("/keys/{key_id}", response_model=AdminKey, responses={404: {"model": ErrorResponse}})
    async def update(
        key_id: int,
        body: UpdateKeyRequest,
        actor: ApiKey = Depends(require_admin),
        session: AsyncSession = Depends(get_session),
    ) -> AdminKey:
        key = await load(session, key_id)
        changes = {}
        if body.name is not None and body.name.strip() and body.name.strip() != key.name:
            changes["name"] = [key.name, body.name.strip()]
            key.name = body.name.strip()
        if body.monthly_credit_limit is not None and body.monthly_credit_limit != key.monthly_credit_limit:
            changes["monthly_credit_limit"] = [key.monthly_credit_limit, body.monthly_credit_limit]
            key.monthly_credit_limit = body.monthly_credit_limit
        if changes:
            await session.commit()
            _audit(actor, "update_key", key, changes=changes)
        return await with_usage(session, key_id)

    @router.post(
        "/keys/{key_id}/revoke",
        response_model=AdminKey,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def revoke(
        key_id: int,
        actor: ApiKey = Depends(require_admin),
        session: AsyncSession = Depends(get_session),
    ) -> AdminKey:
        key = await load(session, key_id)
        if key.active:
            if key.is_admin:
                admins = (
                    await session.execute(
                        select(func.count(ApiKey.id)).where(ApiKey.is_admin, ApiKey.active)
                    )
                ).scalar_one()
                if admins <= 1:
                    raise ApiError(409, "last_admin", "This is the only active admin key. Create another admin key first.")
            key.active = False
            await session.commit()
            _audit(actor, "revoke_key", key, name=key.name)
        return await with_usage(session, key_id)

    @router.get("/usage", response_model=AdminUsage)
    async def usage(
        days: int = Query(default=30, ge=1, le=90),
        _: ApiKey = Depends(require_admin),
        session: AsyncSession = Depends(get_session),
    ) -> AdminUsage:
        totals = await month_totals(session)
        active = (await session.execute(select(func.count(ApiKey.id)).where(ApiKey.active))).scalar_one()
        return AdminUsage(
            period_start=as_utc(month_start()),
            active_keys=int(active),
            daily=await daily_usage(session, days),
            **totals,
        )

    return router
