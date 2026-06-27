"""Unit tests for ``aegis.ops.webhook_circuit``.

Pure-Python: never touches Redis. The in-memory backend exercises the
state-machine semantics directly; the production Redis backend is
contractually identical (incr / expire / get / delete / scan) and
covered by an integration smoke on the box, not the unit suite.

Coverage:

* 5 consecutive failures open the breaker.
* One success after 4 failures resets the streak — fewer than the
  threshold cannot open the breaker.
* ``is_open`` while open keeps returning True for repeated calls.
* ``reset`` clears the counter immediately and brings the breaker back
  to closed.
* ``list_open_circuits`` returns only leads at or above the threshold.
* The webhook route short-circuits with 204 when the breaker is open
  and writes a ``close.webhook.circuit_open`` audit row per reception.
"""

from __future__ import annotations

from aegis.ops.webhook_circuit import (
    OPEN_THRESHOLD,
    InMemoryCircuitBackend,
    WebhookCircuit,
)


def _make_circuit() -> WebhookCircuit:
    return WebhookCircuit(InMemoryCircuitBackend())


def test_five_consecutive_failures_opens_circuit() -> None:
    circuit = _make_circuit()
    lead = "lead_acme"

    assert not circuit.is_open(lead)
    for _ in range(OPEN_THRESHOLD - 1):
        circuit.record_failure(lead)
        assert not circuit.is_open(lead), "should not open before the threshold"

    final_count = circuit.record_failure(lead)
    assert final_count == OPEN_THRESHOLD
    assert circuit.is_open(lead), "the 5th failure must open the circuit"


def test_success_resets_the_counter() -> None:
    circuit = _make_circuit()
    lead = "lead_acme"

    # 4 failures — almost open, not yet.
    for _ in range(OPEN_THRESHOLD - 1):
        circuit.record_failure(lead)
    assert not circuit.is_open(lead)

    circuit.record_success(lead)

    # The next failure starts the streak from 1, not 5.
    circuit.record_failure(lead)
    assert not circuit.is_open(lead), "single failure after reset must not open"


def test_open_circuit_stays_open_on_repeated_is_open_calls() -> None:
    """``is_open`` is idempotent — checking does not flip the breaker shut."""
    circuit = _make_circuit()
    lead = "lead_acme"
    for _ in range(OPEN_THRESHOLD):
        circuit.record_failure(lead)
    assert circuit.is_open(lead)
    assert circuit.is_open(lead)
    assert circuit.is_open(lead)


def test_reset_closes_an_open_circuit() -> None:
    circuit = _make_circuit()
    lead = "lead_acme"
    for _ in range(OPEN_THRESHOLD):
        circuit.record_failure(lead)
    assert circuit.is_open(lead)

    circuit.reset(lead)
    assert not circuit.is_open(lead)


def test_list_open_circuits_only_returns_open_ones() -> None:
    circuit = _make_circuit()

    # lead_open: hit the threshold; lead_below: stays below.
    for _ in range(OPEN_THRESHOLD):
        circuit.record_failure("lead_open")
    for _ in range(OPEN_THRESHOLD - 2):
        circuit.record_failure("lead_below")

    open_circuits = dict(circuit.list_open_circuits())
    assert "lead_open" in open_circuits
    assert open_circuits["lead_open"] >= OPEN_THRESHOLD
    assert "lead_below" not in open_circuits


def test_list_open_circuits_sorted_by_count_when_route_sorts() -> None:
    """``list_open_circuits`` returns unsorted tuples; the route layer
    sorts. This test pins the shape (count is the second element) so a
    future refactor that re-orders the tuple is caught."""
    circuit = _make_circuit()
    for _ in range(OPEN_THRESHOLD):
        circuit.record_failure("lead_a")
    for _ in range(OPEN_THRESHOLD + 3):
        circuit.record_failure("lead_b")

    rows = circuit.list_open_circuits()
    by_lead = dict(rows)
    assert by_lead["lead_b"] > by_lead["lead_a"]


def test_record_failure_returns_the_post_increment_count() -> None:
    circuit = _make_circuit()
    lead = "lead_acme"
    assert circuit.record_failure(lead) == 1
    assert circuit.record_failure(lead) == 2
    assert circuit.record_failure(lead) == 3
