"""Sealed-prediction service: commit-reveal of predictions backed by PostgreSQL.

Commitment scheme (exactly as recomputed by the server during reveal):

    SHA-256( uint64_be(len(UTF-8(payload))) || UTF-8(payload) || UTF-8(salt) )

All deadline decisions use the database server clock (UTC). A commit is
accepted only strictly before commit_deadline; a reveal is accepted in the
inclusive window [commit_deadline, reveal_deadline]. Only the first reveal
whose recomputed digest matches the stored commitment takes effect.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field, field_validator, model_validator

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://seal:seal@db:5432/sealed"
)

# Executed on every startup, so a fresh database needs no manual migration.
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id              UUID PRIMARY KEY,
    commitment      BYTEA NOT NULL,
    payload         TEXT,
    salt            TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    commit_deadline TIMESTAMPTZ NOT NULL,
    reveal_deadline TIMESTAMPTZ NOT NULL,
    revealed_at     TIMESTAMPTZ,
    CONSTRAINT events_deadlines_ordered
        CHECK (reveal_deadline > commit_deadline)
);
"""

EVENT_COLUMNS = """
    id, commitment, payload, salt, created_at,
    commit_deadline, reveal_deadline, revealed_at
"""


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class CreateEventRequest(BaseModel):
    commitment_sha256: str = Field(
        ...,
        description="Hex-encoded SHA-256 commitment: "
        "sha256(uint64be(len(payload_utf8)) || payload_utf8 || salt_utf8)",
    )
    commit_deadline: datetime = Field(
        ..., description="Commitments are accepted strictly before this instant (UTC)."
    )
    reveal_deadline: datetime = Field(
        ..., description="Reveals are accepted up to and including this instant (UTC)."
    )

    @field_validator("commitment_sha256")
    @classmethod
    def _valid_sha256_hex(cls, value: str) -> str:
        if len(value) != 64:
            raise ValueError("commitment_sha256 must be exactly 64 hex characters")
        try:
            bytes.fromhex(value)
        except ValueError:
            raise ValueError("commitment_sha256 must be valid hexadecimal")
        return value.lower()

    @model_validator(mode="after")
    def _deadlines_ordered(self) -> "CreateEventRequest":
        commit_at = _as_utc(self.commit_deadline)
        reveal_at = _as_utc(self.reveal_deadline)
        if reveal_at <= commit_at:
            raise ValueError("reveal_deadline must be later than commit_deadline")
        return self


class RevealRequest(BaseModel):
    payload: str = Field(..., description="The original prediction text.")
    salt: str = Field(..., description="The random salt used when making the commitment.")


def _as_utc(value: datetime) -> datetime:
    """Treat naive timestamps as UTC and convert aware ones to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def compute_commitment(payload: str, salt: str) -> bytes:
    """Recompute the 32-byte commitment: SHA-256 over
    8-byte big-endian payload byte length, payload UTF-8 bytes, salt UTF-8 bytes."""
    payload_bytes = payload.encode("utf-8")
    salt_bytes = salt.encode("utf-8")
    length_prefix = len(payload_bytes).to_bytes(8, byteorder="big", signed=False)
    return hashlib.sha256(length_prefix + payload_bytes + salt_bytes).digest()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The DB container may still be starting; retry in the background until up.
    pool = AsyncConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
    )
    last_error: Exception | None = None
    for attempt in range(60):
        try:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
            last_error = None
            break
        except Exception as exc:  # pool keeps retrying in the background
            last_error = exc
            await asyncio.sleep(1)
    if last_error is not None:
        await pool.close()
        raise RuntimeError(f"database unreachable: {last_error}")

    async with pool.connection() as conn:
        await conn.execute(CREATE_TABLE_SQL)

    app.state.pool = pool
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(
    title="Sealed Predictions API",
    version="1.0.0",
    description="Commit-reveal service for time-sealed predictions.",
    lifespan=lifespan,
)


@app.exception_handler(APIError)
async def handle_api_error(_request: Any, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


def serialize_event(row: dict, now: datetime) -> dict:
    """Build the verifiable event representation, including its timeline."""
    commitment = bytes(row["commitment"])
    revealed_at = row["revealed_at"]

    if revealed_at is not None:
        status = "revealed"
    elif now <= row["reveal_deadline"]:
        # Covers both the open commit period and the open reveal window.
        status = "sealed"
    else:
        status = "expired"

    timeline = [
        {"type": "event_created", "at": row["created_at"], "scheduled": False},
        {"type": "commitment_recorded", "at": row["created_at"], "scheduled": False},
        {"type": "commit_deadline", "at": row["commit_deadline"], "scheduled": True},
        {
            "type": "reveal_window_opens",
            "at": row["commit_deadline"],
            "scheduled": True,
        },
        {"type": "reveal_deadline", "at": row["reveal_deadline"], "scheduled": True},
    ]

    payload = row["payload"]
    salt = row["salt"]
    recomputed_hex: str | None = None
    commitment_matches: bool | None = None
    if revealed_at is not None and payload is not None and salt is not None:
        recomputed = compute_commitment(payload, salt)
        recomputed_hex = recomputed.hex()
        commitment_matches = recomputed == commitment
        timeline.append(
            {"type": "revealed", "at": revealed_at, "scheduled": False}
        )

    return {
        "id": row["id"],
        "status": status,
        "commitment_sha256": commitment.hex(),
        "commitment_algorithm": "SHA-256",
        "commitment_scheme": "sha256(uint64be(utf8_byte_length(payload)) "
        "|| utf8(payload) || utf8(salt))",
        "created_at": row["created_at"],
        "commit_deadline": row["commit_deadline"],
        "reveal_deadline": row["reveal_deadline"],
        "revealed_at": revealed_at,
        "payload": payload,
        "salt": salt,
        "recomputed_commitment_sha256": recomputed_hex,
        "commitment_matches": commitment_matches,
        "server_time": now,
        "timeline": timeline,
    }


@app.get("/health")
async def health() -> dict:
    try:
        async with app.state.pool.connection() as conn:
            await conn.execute("SELECT 1")
    except Exception:
        raise APIError(503, "database_unavailable", "database is not available")
    return {"status": "ok"}


@app.post("/events", status_code=201)
async def create_event(body: CreateEventRequest) -> dict:
    commit_deadline = _as_utc(body.commit_deadline)
    reveal_deadline = _as_utc(body.reveal_deadline)
    event_id = uuid.uuid4()
    commitment = bytes.fromhex(body.commitment_sha256)

    async with app.state.pool.connection() as conn:
        # The clock predicate runs in the database so the deadline is judged
        # atomically at insert time by the server-side UTC clock.
        cur = await conn.execute(
            f"""
            INSERT INTO events
                (id, commitment, created_at, commit_deadline, reveal_deadline)
            SELECT %s, %s, NOW(), %s, %s
            WHERE NOW() < %s
            RETURNING {EVENT_COLUMNS}, NOW() AS server_now
            """,
            [event_id, commitment, commit_deadline, reveal_deadline, commit_deadline],
        )
        row = await cur.fetchone()

    if row is None:
        raise APIError(
            409,
            "commit_deadline_passed",
            "the commit deadline has already passed; no event was created",
        )
    return serialize_event(row, row["server_now"])


@app.get("/events/{event_id}")
async def get_event(event_id: uuid.UUID) -> dict:
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT {EVENT_COLUMNS}, NOW() AS server_now "
            "FROM events WHERE id = %s",
            [event_id],
        )
        row = await cur.fetchone()
    if row is None:
        raise APIError(404, "event_not_found", f"no event with id {event_id}")
    return serialize_event(row, row["server_now"])


@app.post("/events/{event_id}/reveal")
async def reveal_event(event_id: uuid.UUID, body: RevealRequest) -> dict:
    async with app.state.pool.connection() as conn:
        async with conn.transaction():
            # Row lock serializes concurrent reveals: only the first matching
            # one can win.
            cur = await conn.execute(
                f"SELECT {EVENT_COLUMNS}, NOW() AS server_now "
                "FROM events WHERE id = %s FOR UPDATE",
                [event_id],
            )
            row = await cur.fetchone()
            if row is None:
                raise APIError(
                    404, "event_not_found", f"no event with id {event_id}"
                )

            now = row["server_now"]

            if row["revealed_at"] is not None:
                raise APIError(
                    409,
                    "already_revealed",
                    "this event has already been revealed; "
                    "the first matching reveal is final",
                )
            if now < row["commit_deadline"]:
                raise APIError(
                    409,
                    "reveal_before_commit_deadline",
                    "reveals are not accepted before the commit deadline",
                )
            if now > row["reveal_deadline"]:
                raise APIError(
                    409,
                    "reveal_after_reveal_deadline",
                    "the reveal deadline has passed; the event remains sealed",
                )

            recomputed = compute_commitment(body.payload, body.salt)
            if recomputed != bytes(row["commitment"]):
                # Leaving the transaction by exception rolls it back:
                # payload/salt are discarded and no state changes.
                raise APIError(
                    400,
                    "commitment_mismatch",
                    "SHA-256(uint64be(len(payload_utf8)) || payload_utf8 "
                    "|| salt_utf8) does not match the stored commitment",
                )

            cur = await conn.execute(
                """
                UPDATE events
                   SET payload = %s, salt = %s, revealed_at = NOW()
                 WHERE id = %s AND revealed_at IS NULL
                """,
                [body.payload, body.salt, event_id],
            )
            if cur.rowcount != 1:
                # Lost a concurrent reveal race despite the lock check.
                raise APIError(
                    409,
                    "already_revealed",
                    "this event has already been revealed; "
                    "the first matching reveal is final",
                )

            cur = await conn.execute(
                f"SELECT {EVENT_COLUMNS}, NOW() AS server_now "
                "FROM events WHERE id = %s",
                [event_id],
            )
            updated = await cur.fetchone()

    return serialize_event(updated, updated["server_now"])
