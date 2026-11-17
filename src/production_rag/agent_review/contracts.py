"""Integration contracts only: discovery and semantic implementations are NOT supplied.

Any model-backed implementation must run under the caller's shared request/token/
time accounting. These protocols are not automatically invoked by engine.review.
"""
from dataclasses import dataclass
from typing import Literal, Protocol

from .engine import Budget, Evidence, Question, Result, ReviewInput


@dataclass(frozen=True)
class CallLimits:
    budget: Budget
    deadline_monotonic: float
    max_tokens: int


@dataclass(frozen=True)
class DiscoveryOutput:
    status: Literal['completed', 'unknown', 'failed']
    questions: tuple[Question, ...]
    limitations: tuple[str, ...]
    method: Literal['deterministic', 'model', 'manual']
    requests: int
    tokens: int
    elapsed_seconds: float
    error: str = ''


class DoubtDiscovery(Protocol):
    def discover(self, item: ReviewInput, limits: CallLimits) -> DiscoveryOutput:
        """Fixed input only; unknown/failed cannot imply no doubt was found."""
        ...


@dataclass(frozen=True)
class ClaimSupport:
    claim_id: str
    dimension: Literal['contract_fact', 'policy_requirement', 'comparison',
                       'approval_status', 'reason']
    assertion: str
    evidence: tuple[Evidence, ...]
    status: Literal['supported', 'unsupported', 'unknown']
    explanation: str


@dataclass(frozen=True)
class SemanticOutput:
    status: Literal['completed', 'unknown', 'failed']
    claims: tuple[ClaimSupport, ...]
    coverage_complete: bool
    requests: int
    tokens: int
    elapsed_seconds: float
    error: str = ''


class SemanticSupportVerifier(Protocol):
    def verify(self, candidate: Result, item: ReviewInput,
               evidence: tuple[Evidence, ...], limits: CallLimits) -> SemanticOutput:
        """Must independently enumerate all material assertions; no confidence shortcut."""
        ...
