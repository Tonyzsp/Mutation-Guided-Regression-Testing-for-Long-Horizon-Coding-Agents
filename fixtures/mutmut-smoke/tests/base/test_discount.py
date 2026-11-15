from fixture_pkg.discount import discount_rate


def test_amount_below_threshold_has_no_discount():
    assert discount_rate(50) == 0


def test_amount_above_threshold_gets_discount():
    assert discount_rate(150) == 10
