"""ST-19-03: URGENT category ordering per B.13/F410 (security/system > backup/data-loss >
resources > expected task results), and which categories may interrupt at all."""

from __future__ import annotations

import pytest

from nox.proactive.models import INTERRUPT_ELIGIBLE, URGENT_ORDER, UrgentCategory, urgent_sort_key


def test_urgent_order_matches_b13() -> None:
    assert URGENT_ORDER == (
        UrgentCategory.SECURITY,
        UrgentCategory.DATA_LOSS,
        UrgentCategory.RESOURCES,
        UrgentCategory.TASK_RESULT,
    )


def test_urgent_sort_key_orders_a_mixed_batch() -> None:
    batch = [
        UrgentCategory.TASK_RESULT,
        UrgentCategory.SECURITY,
        UrgentCategory.RESOURCES,
        UrgentCategory.DATA_LOSS,
    ]
    ordered = sorted(batch, key=urgent_sort_key)
    assert ordered == [
        UrgentCategory.SECURITY,
        UrgentCategory.DATA_LOSS,
        UrgentCategory.RESOURCES,
        UrgentCategory.TASK_RESULT,
    ]


@pytest.mark.parametrize(
    ("category", "eligible"),
    [
        (UrgentCategory.SECURITY, True),
        (UrgentCategory.DATA_LOSS, True),
        (UrgentCategory.RESOURCES, False),
        (UrgentCategory.TASK_RESULT, False),
    ],
)
def test_only_security_and_data_loss_may_interrupt(
    category: UrgentCategory, eligible: bool
) -> None:
    assert (category in INTERRUPT_ELIGIBLE) is eligible
