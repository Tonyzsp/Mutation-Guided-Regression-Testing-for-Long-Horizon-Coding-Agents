"""A tiny threshold function for checking mutation-test behavior."""


def discount_rate(amount: int) -> int:
    """Return 10 for amounts strictly over 100, otherwise return 0."""
    if amount > 100:
        return 10
    return 0
