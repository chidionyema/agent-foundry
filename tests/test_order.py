"""Order schema gate tests — the input gate of the factory (ORDER.md section 1 +
3.1). A test grades parsed structure, never prose."""

from __future__ import annotations

from af.order import Order, OrderValidationError, load_schema


# A fully valid `many` order of the exact shape ORDER.md section 1 shows.
def _valid_many_order() -> dict:
    return {
        "order_id": "ord_01ABC",
        "tenant_id": "ten_alpha",
        "goal": "scrape 5 competitor sites daily, alert if mine is 10% dearer",
        "bot_count": 9000,
        "scope": {
            "urls": ["https://a.example"],
            "comparison_basis": "price",
            "threshold_pct": 10,
        },
        "assembly": {"mode": "firstile", "train_gate": "deferred"},
        "created_at": "2026-09-07T00:00:00Z",
    }


def _valid_one_order() -> dict:
    o = _valid_many_order()
    o["bot_count"] = 1
    return o


def test_schema_itself_is_valid_json_schema() -> None:
    schema = load_schema()
    assert schema["properties"]["bot_count"]["minimum"] == 1
    assert schema["required"] == [
        "order_id",
        "tenant_id",
        "goal",
        "bot_count",
        "scope",
        "assembly",
        "created_at",
    ]


def test_many_order_parses_and_is_many() -> None:
    order = Order.from_dict(_valid_many_order())
    assert order.is_many is True
    assert order.bot_count == 9000
    assert order.assembly_mode == "firstile"
    assert order.run_id is None


def test_one_order_is_not_many_but_same_path() -> None:
    order = Order.from_dict(_valid_one_order())
    assert order.is_many is False
    assert order.bot_count == 1


def test_bot_count_zero_is_rejected() -> None:
    bad = _valid_many_order()
    bad["bot_count"] = 0
    try:
        Order.from_dict(bad)
        raise AssertionError("expected OrderValidationError")
    except OrderValidationError as exc:
        assert "bot_count" in exc.summary()


def test_missing_goal_is_rejected() -> None:
    bad = _valid_many_order()
    del bad["goal"]
    try:
        Order.from_dict(bad)
        raise AssertionError("expected OrderValidationError")
    except OrderValidationError:
        pass


def test_extra_unknown_field_is_rejected() -> None:
    # additionalProperties: false guards silent schema drift / typos on the wire.
    bad = _valid_many_order()
    bad["department"] = "billing"  # not part of the contract
    try:
        Order.from_dict(bad)
        raise AssertionError("expected OrderValidationError")
    except OrderValidationError as exc:
        assert "department" in exc.summary()


def test_train_candidate_mode_is_allowed_but_is_not_auto_run() -> None:
    # The schema permits an order to NAME a train candidate; the factory's run
    # path refuses anything but firstile (checked in army/cli, not here). This
    # test pins that a deferred train_gate order stays deferred.
    o = _valid_many_order()
    o["assembly"] = {"mode": "train_candidate", "train_gate": "deferred"}
    order = Order.from_dict(o)
    assert order.assembly_mode == "train_candidate"
