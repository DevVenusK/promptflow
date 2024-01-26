# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

import typing

from sqlalchemy import TEXT, Column, Index
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base

from promptflow._sdk._constants import SPAN_TABLENAME

from .retry import sqlite_retry
from .session import trace_mgmt_db_session

Base = declarative_base()


class Span(Base):
    __tablename__ = SPAN_TABLENAME

    id = Column(TEXT, primary_key=True)
    name = Column(TEXT, nullable=False)
    type = Column(TEXT, nullable=False)  # span type: Function/Tool/Flow/LLM/LangChain...
    trace_id = Column(TEXT, nullable=False)
    parent_span_id = Column(TEXT, nullable=True)
    session_id = Column(TEXT, nullable=False)
    content = Column(TEXT)  # JSON string
    # prompt flow concepts
    path = Column(TEXT, nullable=True)
    run = Column(TEXT, nullable=True)
    experiment = Column(TEXT, nullable=True)

    __table_args__ = (
        Index("idx_span_name", "name"),
        Index("idx_span_type", "type"),
        Index("idx_span_session_id", "session_id"),
        Index("idx_span_run", "run"),
        Index("idx_span_experiment", "experiment"),
    )

    @sqlite_retry
    def persist(self) -> None:
        with trace_mgmt_db_session() as session:
            try:
                session.add(self)
                session.commit()
            except IntegrityError as e:
                # ignore "sqlite3.IntegrityError: UNIQUE constraint failed"
                # according to OTLP 1.1.0: https://opentelemetry.io/docs/specs/otlp/#duplicate-data
                # there might be duplicate data, we silently ignore it here
                if "UNIQUE constraint failed" not in str(e):
                    raise

    @staticmethod
    @sqlite_retry
    def get(span_id: str) -> "Span":
        with trace_mgmt_db_session() as session:
            return session.query(Span).filter(Span.id == span_id).first()

    @staticmethod
    @sqlite_retry
    def list() -> typing.List["Span"]:
        with trace_mgmt_db_session() as session:
            basic_stmt = session.query(Span)
            # TODO: refine the query condition
            return [span for span in basic_stmt.limit(100)]
