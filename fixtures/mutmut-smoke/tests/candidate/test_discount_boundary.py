from fixture_pkg.discount import discount_rate


def test_exact_threshold_has_no_discount():
    assert discount_rate(100) == 0
