"""Stateless policies for sizing virtualized row pools."""


def _initial_catalog_pool_target(total: int, existing: int, initial: int = 10) -> int:
    """Keep the initial widget pool bounded while retaining reusable rows."""
    return min(total, max(existing, initial))


def _queue_pool_size(
    available_height: int,
    *,
    row_height: int = 40,
    minimum: int = 10,
    overscan: int = 4,
    maximum: int = 20,
) -> int:
    """Derive the bounded physical row pool from the visible queue height."""
    visible = max(1, available_height // max(1, row_height))
    return min(maximum, max(minimum, visible + overscan))


def _queue_model_count(pool_target: int, existing_rows: int) -> int:
    """Keep surplus pooled rows addressable so a smaller page can hide them."""
    return max(pool_target, existing_rows)
