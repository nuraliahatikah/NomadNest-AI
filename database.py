"""SQLite persistence for NomadNest AI simulations."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./nomadnest.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}, future=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Location(Base):
    __tablename__ = "nn_locations"
    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(String(300), index=True)
    display_name: Mapped[str] = mapped_column(String(500))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    runs: Mapped[list[SimulationRun]] = relationship(back_populates="location")


class SimulationRun(Base):
    __tablename__ = "nn_simulation_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("nn_locations.id"), index=True)
    run_type: Mapped[str] = mapped_column(String(40))
    model_name: Mapped[str] = mapped_column(String(100))
    agent_source: Mapped[str] = mapped_column(String(40))
    poi_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    location: Mapped[Location] = relationship(back_populates="runs")
    manifest: Mapped[SimulationManifest | None] = relationship(back_populates="run", uselist=False)


class SimulationManifest(Base):
    __tablename__ = "nn_simulation_manifests"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("nn_simulation_runs.id"), unique=True, index=True)
    timeline_json: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    score: Mapped[int] = mapped_column(Integer)
    run: Mapped[SimulationRun] = relationship(back_populates="manifest")


def initialize_database() -> None:
    Base.metadata.create_all(engine)


@contextmanager
def transaction() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def persist_simulation(*, query: str, display_name: str, latitude: float, longitude: float, run_type: str, model_name: str, agent_source: str, poi_snapshot: dict[str, Any], timeline: list[dict[str, Any]], summary: str, score: int) -> int:
    with transaction() as session:
        location = Location(query=query, display_name=display_name, latitude=latitude, longitude=longitude)
        session.add(location)
        session.flush()
        run = SimulationRun(location_id=location.id, run_type=run_type, model_name=model_name, agent_source=agent_source, poi_snapshot=poi_snapshot)
        session.add(run)
        session.flush()
        session.add(SimulationManifest(run_id=run.id, timeline_json=json.dumps(timeline), summary=summary, score=score))
        session.flush()
        return run.id
