from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from uuid import uuid4
import psycopg
from semora_contracts import QueryRun
from .control_state import run_payload, restore_run


@dataclass
class Session:
    id: str
    title: str
    created_at: datetime
    domain: str = ""
    owner_subject: str | None = None
    run_ids: list[str] = field(default_factory=list)
    requirement_form: dict[str, str] = field(default_factory=dict)


class Store:
    def __init__(self) -> None:
        self._dsn = os.getenv("METADATA_DATABASE_URL")
        seed_id = "demo-session"
        self.sessions: dict[str, Session] = {}
        if not self._dsn or os.getenv("SEMORA_DEMO_SEED", "false").lower() == "true":
            self.sessions[seed_id] = Session(
                seed_id, "新查询", datetime.now(timezone.utc), "", None
            )
        self.runs: dict[str, QueryRun] = {}
        if self._dsn:
            self._hydrate()

    def _connect(self):
        return psycopg.connect(self._dsn, autocommit=True)

    def _hydrate(self) -> None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, title, created_at, domain, owner_subject, requirement_form FROM semora_meta.sessions WHERE deleted_at IS NULL ORDER BY created_at"
                )
                for row in cursor.fetchall():
                    self.sessions[row[0]] = Session(row[0], row[1], row[2], row[3], row[4])
                    self.sessions[row[0]].requirement_form = row[5] or {}
                cursor.execute("""SELECT r.payload FROM semora_meta.runs r
                    JOIN semora_meta.sessions s ON s.id=r.session_id
                    WHERE s.deleted_at IS NULL ORDER BY r.created_at""")
                for (payload,) in cursor.fetchall():
                    run = restore_run(payload)
                    self.runs[run.id] = run
                    self.sessions.setdefault(
                        run.session_id,
                        Session(
                            run.session_id,
                            run.question[:24],
                            run.created_at,
                            run.domain,
                            run.authorization.subject if run.authorization else None,
                        ),
                    ).run_ids.append(run.id)

        except Exception as exc:
            raise RuntimeError(f"metadata database initialization failed: {exc}") from exc

    def _persist_session(self, session: Session) -> None:
        if not self._dsn:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO semora_meta.sessions(id,title,created_at,domain,owner_subject,requirement_form)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,domain=excluded.domain,
                owner_subject=coalesce(semora_meta.sessions.owner_subject,excluded.owner_subject),
                requirement_form=excluded.requirement_form""",
                (
                    session.id,
                    session.title,
                    session.created_at,
                    session.domain,
                    session.owner_subject,
                    json.dumps(session.requirement_form, ensure_ascii=False),
                ),
            )

    def create_session(
        self,
        title: str = "新查询",
        domain: str = "",
        owner_subject: str | None = None,
    ) -> Session:
        session = Session(str(uuid4()), title, datetime.now(timezone.utc), domain, owner_subject)
        self.sessions[session.id] = session
        self._persist_session(session)
        return session

    def archive_session(self, session: Session) -> None:
        if self._dsn:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE semora_meta.sessions SET deleted_at=now() WHERE id=%s AND deleted_at IS NULL",
                    (session.id,),
                )
        self.sessions.pop(session.id, None)
        for run_id in session.run_ids:
            self.runs.pop(run_id, None)

    def add_run(self, run: QueryRun) -> None:
        self.runs[run.id] = run
        self.sessions.setdefault(
            run.session_id,
            Session(
                run.session_id,
                run.question[:24],
                datetime.now(timezone.utc),
                run.domain,
                run.authorization.subject if run.authorization else None,
            ),
        ).run_ids.append(run.id)
        self._persist_session(self.sessions[run.session_id])
        self.save_run(run)

    def save_run(self, run: QueryRun) -> None:
        if not self._dsn:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO semora_meta.runs(id,session_id,status,payload,created_at,updated_at)
                VALUES(%s,%s,%s,%s::jsonb,%s,now())
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload,updated_at=now()""",
                (
                    run.id,
                    run.session_id,
                    run.status.value,
                    json.dumps(run_payload(run), ensure_ascii=False),
                    run.created_at,
                ),
            )


store = Store()
