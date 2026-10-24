import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import shipping_estimate  # noqa: E402

ORDER_LB = 2.0


def samples_at(*weights, cost=10.0):
    return [(w, cost) for w in weights]


class PickSamplesTest(unittest.TestCase):
    def test_tight_band_wins_when_it_has_enough(self):
        samples = samples_at(1.95, 2.0, 2.1, 2.9)
        self.assertEqual(shipping_estimate.pick_samples(samples, ORDER_LB), (0.1, [10.0, 10.0, 10.0]))

    def test_widens_to_the_next_band_when_short(self):
        samples = samples_at(2.0, 2.2, 1.8, 5.0)
        self.assertEqual(shipping_estimate.pick_samples(samples, ORDER_LB)[0], 0.25)

    def test_band_edges_are_inclusive(self):
        samples = samples_at(1.5, 2.5, 2.0)
        self.assertEqual(shipping_estimate.pick_samples(samples, ORDER_LB), (0.5, [10.0, 10.0, 10.0]))

    def test_widest_band_is_used_even_with_fewer_than_the_minimum(self):
        samples = samples_at(2.9)
        self.assertEqual(shipping_estimate.pick_samples(samples, ORDER_LB), (1.0, [10.0]))

    def test_nothing_within_the_widest_band(self):
        self.assertEqual(shipping_estimate.pick_samples(samples_at(9.0), ORDER_LB), (None, []))

    def test_heavy_orders_widen_relative_to_their_weight(self):
        self.assertEqual(shipping_estimate.tolerance_tiers_for(200.0), [0.1, 0.25, 0.5, 1.0, 10.0, 20.0, 40.0, 70.0])
        self.assertEqual(shipping_estimate.tolerance_tiers_for(ORDER_LB), [0.1, 0.25, 0.5, 1.0])


class EstimateFromSamplesTest(unittest.TestCase):
    def test_average_is_rounded_to_cents(self):
        samples = [(2.0, 10.0), (2.0, 10.0), (2.05, 10.01)]
        self.assertEqual(shipping_estimate.estimate_from_samples(samples, ORDER_LB),
                         {"cost": 10.0, "samples": 3, "tolerance_lb": 0.1})

    def test_no_match_gives_no_estimate(self):
        self.assertEqual(shipping_estimate.estimate_from_samples([], ORDER_LB),
                         {"cost": None, "samples": 0, "tolerance_lb": None})


class ItemsWeightTest(unittest.TestCase):
    def test_per_unit_weight_times_quantity_from_json_or_list(self):
        items = [{"weight": 0.5, "quantity": 3}, {"weight": "1.25", "quantity": "2"}, {"weight": None, "quantity": 4}]
        self.assertEqual(
            (shipping_estimate.items_weight_lb(items),
             shipping_estimate.items_weight_lb('[{"weight": 0.5, "quantity": 2}]')),
            (4.0, 1.0))

    def test_unreadable_items_weigh_nothing(self):
        self.assertEqual([shipping_estimate.items_weight_lb(v) for v in (None, "not json", [None, "x"])],
                         [0.0, 0.0, 0.0])


class EstimateShippingTest(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.rows = []
        self._orig = shipping_estimate.db.query
        shipping_estimate.db.query = lambda sql, params=None, one=False: self.queries.append(params) or self.rows

    def tearDown(self):
        shipping_estimate.db.query = self._orig

    def test_past_labels_of_the_store_and_state_make_the_estimate(self):
        self.rows = [{"cost": 8.0, "items": [{"weight": 1.0, "quantity": 2}]},
                     {"cost": 9.0, "items": [{"weight": 2.0, "quantity": 1}]},
                     {"cost": 10.0, "items": '[{"weight": 0.5, "quantity": 4}]'},
                     {"cost": 99.0, "items": []}]
        self.assertEqual(shipping_estimate.estimate_shipping(1, " ct ", ORDER_LB), {
            "cost": 9.0, "samples": 3, "tolerance_lb": 0.1, "state": "CT", "weight_lb": 2.0})
        self.assertEqual(self.queries, [(1, "CT", shipping_estimate.SAMPLE_WINDOW_DAYS)])

    def test_missing_state_or_weight_skips_the_query(self):
        results = [shipping_estimate.estimate_shipping(1, state, weight)["cost"]
                   for state, weight in (("", ORDER_LB), ("CT", 0), ("CT", None))]
        self.assertEqual((results, self.queries), ([None, None, None], []))

    def test_a_failing_query_gives_no_estimate(self):
        def boom(*a, **k):
            raise RuntimeError("db down")
        shipping_estimate.db.query = boom
        self.assertEqual(shipping_estimate.estimate_shipping(1, "CT", ORDER_LB)["cost"], None)


if __name__ == "__main__":
    unittest.main()
