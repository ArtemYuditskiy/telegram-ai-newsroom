"""Allowed transitions for the three editorial workflows."""

from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID


class InvalidTransition(ValueError):
    """The requested action does not apply to the current state."""


class CandidateState(StrEnum):
    DISCOVERED = "discovered"
    NORMALIZED = "normalized"
    ELIGIBLE = "eligible"
    SKIPPED = "skipped"
    SCORED = "scored"
    SELECTED = "selected"
    REJECTED = "rejected"
    DRAFTED = "drafted"


class DraftState(StrEnum):
    REVIEW_PENDING = "review_pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PublicationState(StrEnum):
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


def _require_transition[S: StrEnum](current: S, target: S, allowed: set[S]) -> None:
    if type(current) is not type(target) or target not in allowed:
        raise InvalidTransition(f"Cannot transition from {current} to {target}")


@dataclass(frozen=True, slots=True)
class Candidate:
    id: UUID
    state: CandidateState = CandidateState.DISCOVERED

    def transition(self, target: CandidateState) -> "Candidate":
        allowed = {
            CandidateState.DISCOVERED: {CandidateState.NORMALIZED},
            CandidateState.NORMALIZED: {CandidateState.ELIGIBLE, CandidateState.SKIPPED},
            CandidateState.ELIGIBLE: {CandidateState.SCORED},
            CandidateState.SCORED: {CandidateState.SELECTED, CandidateState.REJECTED},
            CandidateState.SELECTED: {CandidateState.DRAFTED},
        }
        _require_transition(self.state, target, allowed.get(self.state, set()))
        return replace(self, state=target)


@dataclass(frozen=True, slots=True)
class DraftRevision:
    id: UUID
    candidate_id: UUID
    text: str
    state: DraftState = DraftState.REVIEW_PENDING

    @classmethod
    def create(cls, id: UUID, candidate: Candidate, text: str) -> "DraftRevision":
        if candidate.state not in {CandidateState.SELECTED, CandidateState.DRAFTED}:
            raise InvalidTransition("Only selected or drafted candidates can get draft revisions")
        return cls(id=id, candidate_id=candidate.id, text=text)

    def review(self, target: DraftState) -> "DraftRevision":
        allowed = (
            {DraftState.APPROVED, DraftState.REJECTED}
            if self.state == DraftState.REVIEW_PENDING
            else set()
        )
        _require_transition(self.state, target, allowed)
        return replace(self, state=target)


@dataclass(frozen=True, slots=True)
class Publication:
    id: UUID
    draft_revision_id: UUID
    state: PublicationState = PublicationState.SCHEDULED

    @classmethod
    def schedule(cls, id: UUID, draft: DraftRevision) -> "Publication":
        if draft.state != DraftState.APPROVED:
            raise InvalidTransition("Only approved draft revisions can be scheduled")
        return cls(id=id, draft_revision_id=draft.id)

    def transition(self, target: PublicationState) -> "Publication":
        allowed = {
            PublicationState.SCHEDULED: {PublicationState.PUBLISHING},
            PublicationState.RETRY_WAIT: {PublicationState.PUBLISHING},
            PublicationState.PUBLISHING: {
                PublicationState.PUBLISHED,
                PublicationState.RETRY_WAIT,
                PublicationState.FAILED,
                PublicationState.UNCERTAIN,
            },
        }
        _require_transition(self.state, target, allowed.get(self.state, set()))
        return replace(self, state=target)

    def reconcile(self, target: PublicationState) -> "Publication":
        allowed = (
            {PublicationState.PUBLISHED, PublicationState.RETRY_WAIT, PublicationState.FAILED}
            if self.state == PublicationState.UNCERTAIN
            else set()
        )
        _require_transition(self.state, target, allowed)
        return replace(self, state=target)
