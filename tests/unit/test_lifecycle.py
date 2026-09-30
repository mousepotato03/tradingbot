import pytest

from app.discovery import Discovery
from app.engine import ResearchEngine
from app.lifecycle import transition
from app.models import CandidateState as State
from app.storage import StateRow


@pytest.mark.parametrize("state", list(State))
def test_unchanged_state_is_idempotent(state):
    assert transition(state, state) == state


@pytest.mark.parametrize(
    "old,new",
    [
        (State.UNIVERSE, State.RESEARCH),
        (State.RESEARCH, State.WATCH),
        (State.RESEARCH, State.ENTRY),
        (State.WATCH, State.ENTRY),
        (State.ENTRY, State.WATCH),
        (State.WATCH, State.RESEARCH),
        (State.ENTRY, State.ACTIVE),
        (State.WATCH, State.ACTIVE),
        (State.ACTIVE, State.EXITED),
        (State.EXITED, State.RESEARCH),
        (State.INVALIDATED, State.RESEARCH),
        (State.RESEARCH, State.INVALIDATED),
    ],
)
def test_research_entry_and_actual_holding_lifecycle(old, new):
    assert transition(old, new) == new


@pytest.mark.parametrize(
    "old,new",
    [
        (State.UNIVERSE, State.ENTRY),
        (State.UNIVERSE, State.EXITED),
        (State.WATCH, State.EXITED),
        (State.ACTIVE, State.WATCH),
        (State.ACTIVE, State.INVALIDATED),
        (State.INVALIDATED, State.ENTRY),
    ],
)
def test_cannot_skip_research_or_fabricate_a_position_exit(old, new):
    with pytest.raises(ValueError):
        transition(old, new)


def test_discovery_keeps_verified_universe_and_rotates_batch(store, settings):
    discovery = Discovery(ResearchEngine(settings, store))
    first, second = discovery.scan(1), discovery.scan(1)
    assert len(first) == len(second) == 1 and first != second
    assert not discovery.scan(1)
    with store.transaction() as session:
        assert session.get(StateRow, "TEST").state == State.RESEARCH
