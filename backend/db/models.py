import uuid
from sqlalchemy import (
    Boolean, Column, DateTime, Index, Integer, String, Text,
    ForeignKey, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Program(Base):
    __tablename__ = "programs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    domain = Column(String(100), nullable=False)
    context_config = Column(JSONB, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    sprints = relationship("Sprint", back_populates="program")
    tickets = relationship("Ticket", back_populates="program")
    agent_decisions = relationship("AgentDecision", back_populates="program")
    executive_outputs = relationship("ExecutiveOutput", back_populates="program")
    operational_memory = relationship("OperationalMemory", back_populates="program")


class Sprint(Base):
    __tablename__ = "sprints"

    id = Column(String(100), primary_key=True)
    program_id = Column(UUID(as_uuid=True), ForeignKey("programs.id"), nullable=False)
    name = Column(String(255), nullable=False)
    start_date = Column(DateTime(timezone=True))
    end_date = Column(DateTime(timezone=True))
    health_badge = Column(String(10))       # HEALTHY|WATCH|ALERT|ESCALATE
    worst_severity = Column(String(10))     # LOW|MEDIUM|HIGH|CRITICAL
    last_run_id = Column(String(100))       # ties health to the cycle that set it
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    program = relationship("Program", back_populates="sprints")
    tickets = relationship("Ticket", back_populates="sprint")


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(String(100), primary_key=True)
    program_id = Column(UUID(as_uuid=True), ForeignKey("programs.id"), nullable=False)
    title = Column(String(500), nullable=False)
    description = Column(Text)
    status = Column(String(20), nullable=False)     # TODO|IN_PROGRESS|IN_REVIEW|BLOCKED|DONE
    priority = Column(String(5), nullable=False)    # P0|P1|P2|P3
    assignee = Column(String(255))
    team = Column(String(255))
    sprint_id = Column(String(100), ForeignKey("sprints.id"))
    story_points = Column(Integer, default=0)
    points_completed = Column(Integer, default=0)
    is_on_critical_path = Column(Boolean, default=False)
    blocker_ids = Column(JSONB, default=list)
    stale_since = Column(DateTime(timezone=True))
    owner_changed_at = Column(DateTime(timezone=True))
    scope_changed = Column(Boolean, default=False)
    milestone_target = Column(String(255))
    risk_flag = Column(String(20))      # STALE|BLOCKED|SCOPE_CREEP|OVERLOADED
    risk_severity = Column(String(10))  # LOW|MEDIUM|HIGH|CRITICAL
    risk_reason = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_tickets_program_sprint", "program_id", "sprint_id"),
        Index("ix_tickets_program_status", "program_id", "status"),
        Index("ix_tickets_assignee_status", "assignee", "status"),
    )

    program = relationship("Program", back_populates="tickets")
    sprint = relationship("Sprint", back_populates="tickets")


class AgentDecision(Base):
    __tablename__ = "agent_decisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    program_id = Column(UUID(as_uuid=True), ForeignKey("programs.id"), nullable=False)
    domain = Column(String(100), nullable=False)    # copied from program.domain
    run_id = Column(String(100), nullable=False)    # ties to pipeline cycle
    cycle_number = Column(Integer, nullable=False)
    agent_name = Column(String(100), nullable=False)
    decision = Column(Text, nullable=False)
    reasoning = Column(Text, nullable=False)
    input_summary = Column(JSONB)
    output_summary = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_agent_decisions_program_run", "program_id", "run_id"),
        Index("ix_agent_decisions_program_created", "program_id", "created_at"),
    )

    program = relationship("Program", back_populates="agent_decisions")


class ExecutiveOutput(Base):
    __tablename__ = "executive_outputs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    program_id = Column(UUID(as_uuid=True), ForeignKey("programs.id"), nullable=False)
    domain = Column(String(100), nullable=False)
    run_id = Column(String(100), nullable=False)
    cycle_number = Column(Integer, nullable=False)
    output_type = Column(String(30), nullable=False)    # STANDUP_SUMMARY|ESCALATION_MEMO|RISK_DIGEST
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_executive_outputs_program_type_created", "program_id", "output_type", "created_at"),
    )

    program = relationship("Program", back_populates="executive_outputs")


class OperationalMemory(Base):
    __tablename__ = "operational_memory"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    program_id = Column(UUID(as_uuid=True), ForeignKey("programs.id"), nullable=False)
    domain = Column(String(100), nullable=False)
    key = Column(String(255), nullable=False)       # e.g. 'cycle_counter'
    value = Column(JSONB, nullable=False)
    expires_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("program_id", "key", name="uq_operational_memory_program_key"),
        Index("ix_operational_memory_program_key", "program_id", "key"),
    )

    program = relationship("Program", back_populates="operational_memory")
