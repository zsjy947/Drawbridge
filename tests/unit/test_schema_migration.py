"""Schema v2 migration tests (plan D3 + D12 column set).

A version-1 database (pre template-fingerprint, pre simulated flag) must be
upgraded in place: new plan columns appear NULL-compatible, the releases
flag appears with default 0, and pre-existing simulation releases are
backfilled via their evidence marker.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from drawbridge.state.db import Database
from drawbridge.state.schema import SCHEMA_VERSION


async def _make_v1_database(path: Path) -> None:
    """Hand-build a version-1 database with one legacy release row."""
    conn = await aiosqlite.connect(path)
    try:
        await conn.execute(
            """
            CREATE TABLE control_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
            )
            """
        )
        await conn.execute(
            "INSERT INTO control_state VALUES('schema_version', '1', 0.0)"
        )
        await conn.execute(
            """
            CREATE TABLE releases (
                release_id TEXT PRIMARY KEY, app TEXT NOT NULL,
                environment TEXT NOT NULL, plan_id TEXT, job_id TEXT,
                commit_sha TEXT, image_id TEXT, image_tag TEXT,
                config_digest TEXT NOT NULL, status TEXT NOT NULL,
                rollback_of TEXT, compose_path TEXT, deploy_dir TEXT,
                evidence_json TEXT, created_at REAL NOT NULL, verified_at REAL
            )
            """
        )
        # normalized evidence exactly as records.dumps would write it
        sim_evidence = (
            '{"checks":[{"passed":true,"validation_level":"simulation"}],'
            '"verified_at":123.0}'
        )
        prod_evidence = '{"health":{"passed":true},"verified_at":124.0}'
        await conn.execute(
            "INSERT INTO releases(release_id, app, environment, config_digest,"
            " status, evidence_json, created_at) VALUES(?,?,?,?,?,?,?)",
            ("r-sim", "demo", "staging", "d" * 64, "succeeded", sim_evidence, 1.0),
        )
        await conn.execute(
            "INSERT INTO releases(release_id, app, environment, config_digest,"
            " status, evidence_json, created_at) VALUES(?,?,?,?,?,?,?)",
            ("r-prod", "demo", "staging", "d" * 64, "succeeded", prod_evidence, 2.0),
        )
        await conn.execute(
            """
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY, app TEXT NOT NULL,
                environment TEXT NOT NULL, workflow TEXT NOT NULL,
                source_mode TEXT NOT NULL, git_ref TEXT NOT NULL, commit_sha TEXT,
                config_digest TEXT NOT NULL, baseline_release_id TEXT,
                status TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, params_json TEXT NOT NULL, request_id TEXT
            )
            """
        )
        await conn.execute(
            "INSERT INTO plans(plan_id, app, environment, workflow, source_mode,"
            " git_ref, config_digest, status, created_at, expires_at, params_json)"
            " VALUES('p-legacy','demo','staging','deploy_verify','fetch',"
            "'refs/heads/main','d'||'d','planned',1.0,2.0,'{}')"
        )
        await conn.commit()
    finally:
        await conn.close()


class TestSchemaV2Migration:
    async def test_fresh_database_is_v2(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db")
        await db.connect()
        await db.initialize()
        try:
            version = await db.get_control("schema_version")
            assert version == str(SCHEMA_VERSION)
            assert int(version) == 2
        finally:
            await db.close()

    async def test_v1_database_migrates_in_place(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        await _make_v1_database(path)
        db = Database(path)
        await db.connect()
        await db.initialize()
        try:
            assert await db.get_control("schema_version") == "2"
            # plan columns exist and legacy rows read back NULL-compatible
            from drawbridge.state.records import row_to_plan, row_to_release

            async with db.conn.execute(
                "SELECT * FROM plans WHERE plan_id = 'p-legacy'"
            ) as cursor:
                plan = row_to_plan(await cursor.fetchone())
            assert plan.compose_template_digest is None
            assert plan.plan_schema_version is None
            # simulation release backfilled via evidence marker; prod not
            async with db.conn.execute(
                "SELECT * FROM releases WHERE release_id IN ('r-sim','r-prod')"
                " ORDER BY release_id"
            ) as cursor:
                rows = await cursor.fetchall()
            by_id = {row["release_id"]: row_to_release(row) for row in rows}
            assert by_id["r-sim"].simulated is True
            assert by_id["r-prod"].simulated is False
        finally:
            await db.close()

    async def test_unknown_future_version_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        db = Database(path)
        await db.connect()
        await db.initialize()
        await db.close()
        conn = await aiosqlite.connect(path)
        try:
            await conn.execute(
                "UPDATE control_state SET value = '99' WHERE key = 'schema_version'"
            )
            await conn.commit()
        finally:
            await conn.close()
        db2 = Database(path)
        await db2.connect()
        with pytest.raises(RuntimeError, match="schema version mismatch"):
            await db2.initialize()
        await db2.close()
