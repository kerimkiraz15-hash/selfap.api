from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (Neon connection string).")
    return url


def _init_db() -> None:
    ddl = """
    create table if not exists shares (
      code text primary key,
      payload jsonb not null,
      created_at timestamptz not null default now(),
      expires_at timestamptz not null
    );
    create index if not exists shares_expires_idx on shares (expires_at);
    """
    with psycopg.connect(_db_url(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _init_db()
    yield


app = FastAPI(title="Share API", version="0.1.0", lifespan=lifespan)

TTL_HOURS = int(os.environ.get("TTL_HOURS", "72"))  # 3 days default
MAX_PAYLOAD_BYTES = int(os.environ.get("MAX_PAYLOAD_BYTES", "20000"))  # ~20KB
CODE_BYTES = int(os.environ.get("CODE_BYTES", "9"))  # token_urlsafe(9) ~ 12 chars


class ShareCreateIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_name: str = Field(min_length=1, max_length=200)
    language: str = Field(min_length=1, max_length=50)
    code: str = Field(min_length=1, max_length=200_000)  # project source text


class ShareCreateOut(BaseModel):
    share_code: str
    expires_at: datetime


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/create", response_model=ShareCreateOut)
def create_share(payload: ShareCreateIn) -> ShareCreateOut:
    raw = payload.model_dump()
    b = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(b) > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    now = _utcnow()
    expires_at = now + timedelta(hours=TTL_HOURS)

    # Retry on rare collisions.
    for _ in range(8):
        share_code = secrets.token_urlsafe(CODE_BYTES)
        try:
            with psycopg.connect(_db_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "insert into shares (code, payload, expires_at) values (%s, %s::jsonb, %s)",
                        (share_code, json.dumps(raw, ensure_ascii=False), expires_at),
                    )
                conn.commit()
            return ShareCreateOut(share_code=share_code, expires_at=expires_at)
        except psycopg.errors.UniqueViolation:
            continue

    raise HTTPException(status_code=500, detail="Could not generate unique code")


@app.get("/r/{share_code}")
def resolve_share(share_code: str) -> Any:
    # One-time semantics: atomically delete and return the payload if still valid.
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "delete from shares where code = %s and expires_at > now() returning payload",
                (share_code,),
            )
            row = cur.fetchone()
            if row is not None:
                conn.commit()
                return row[0]

            # Not found or expired. If expired, delete it and return 410.
            cur.execute("select 1 from shares where code = %s", (share_code,))
            exists = cur.fetchone() is not None
            if exists:
                cur.execute("delete from shares where code = %s", (share_code,))
                conn.commit()
                raise HTTPException(status_code=410, detail="Code expired or already used")

            conn.commit()
            raise HTTPException(status_code=404, detail="Code not found")

