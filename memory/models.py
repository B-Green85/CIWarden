"""Pydantic models for the Generational Memory Gate."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ContractType(StrEnum):
    INTERFACE = "interface"
    FUNCTION = "function"
    CLASS = "class"
    MODULE = "module"
    DEPENDENCY = "dependency"


class ContractEntry(BaseModel):
    type: ContractType
    name: str
    fields: list[str] = Field(default_factory=list)
    consumed_by: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)


class ContractSummary(BaseModel):
    module: str
    generation_id: str
    sequence: int
    contracts: list[ContractEntry] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    exposes: list[str] = Field(default_factory=list)
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DriftResult(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    passed: bool
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    detail: str = ""


class Checkpoint(BaseModel):
    session_id: str
    generation_id: str
    sequence: int
    contracts: list[ContractSummary] = Field(default_factory=list)
    drift_history: list[DriftResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Session(BaseModel):
    session_id: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    current_generation_id: str
    current_sequence: int = 0
    last_checkpoint: str | None = None
