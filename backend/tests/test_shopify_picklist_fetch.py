import sys
import threading
import types
import unittest
from datetime import datetime, timezone

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))
sys.modules.setdefault("config", types.SimpleNamespace(
    SHOPIFY_API_VERSION="2026-01", EASYSHIP_BASE_URLS={}, LABELS_DIR="/tmp"))

import shopify_client  # noqa: E402

STORE = {"id": 1, "shop_domain": "x.myshopify.com", "access_token": "t"}
NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)


def line(title):
    return {"title": title, "sku": title, "quantity": 1, "unfulfilledQuantity": 1,
            "requiresShipping": True, "variant": {"barcode": title}}


def order(gid, titles=("A",), more=False):
    return {"id": gid, "lineItems": {"pageInfo": {"hasNextPage": more, "endCursor": "c1" if more else None},
                                     "nodes": [line(t) for t in titles]}}


def page(nodes, after=None):
    return {"orders": {"pageInfo": {"hasNextPage": after is not None, "endCursor": after}, "nodes": nodes}}


class OrderWindowLinesTest(unittest.TestCase):
    def setUp(self):
        self._orig = shopify_client._graphql

    def tearDown(self):
        shopify_client._graphql = self._orig

    def test_order_window_pages_past_the_first_hundred_lines(self):
        def graphql(store_id, query, variables=None):
            if query is shopify_client.ORDER_FRAUD_QUERY:
                return {"order": {"id": "g1", "lineItems": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                                            "nodes": [line("A"), line("B")]}}}
            return {"order": {"lineItems": {"pageInfo": {"hasNextPage": False}, "nodes": [line("C")]}}}

        shopify_client._graphql = graphql
        node = shopify_client.get_order_fraud(1, "g1")
        self.assertEqual([li["title"] for li in node["lineItems"]["nodes"]], ["A", "B", "C"])


class FetchTest(unittest.TestCase):
    def setUp(self):
        self._orig = (shopify_client._store, shopify_client._graphql_for, shopify_client.PICKLIST_WINDOWS)
        shopify_client._store = lambda store_id: STORE
        self.calls = []
        self.lock = threading.Lock()

    def tearDown(self):
        shopify_client._store, shopify_client._graphql_for, shopify_client.PICKLIST_WINDOWS = self._orig

    def serve(self, answer):
        def graphql_for(store, query, variables=None):
            with self.lock:
                self.calls.append((query, dict(variables or {})))
            return answer(query, variables or {})
        shopify_client._graphql_for = graphql_for

    def queries(self, query):
        return [v for q, v in self.calls if q is query]

    def test_windows_are_read_side_by_side_and_an_order_seen_twice_counts_once(self):
        shopify_client.PICKLIST_WINDOWS = 3
        all_running = threading.Barrier(3, timeout=2)

        def answer(query, variables):
            if variables.get("after") is None:
                all_running.wait()  # breaks (and errors) unless every window runs at once
            if "created_at:<" not in variables["query"]:  # the newest window has two pages
                return page([order("g3")], after="p2") if variables.get("after") is None else page([order("g4")])
            return page([order("g1"), order("g2")]) if "'2026-09-26" in variables["query"] else page([order("g2")])

        self.serve(answer)
        nodes = shopify_client.list_picklist_orders(1, NOW)
        self.assertEqual(sorted(n["id"] for n in nodes), ["g1", "g2", "g3", "g4"])

    def test_page_over_the_cost_limit_is_asked_for_again_at_half_the_size(self):
        def answer(query, variables):
            if variables["first"] > 5:
                raise shopify_client.ShopifyCostExceeded("too costly")
            return page([order("g1")])

        self.serve(answer)
        nodes = shopify_client._order_pages(STORE, "q", "CREATED_AT")
        self.assertEqual(([n["id"] for n in nodes], [v["first"] for _, v in self.calls]),
                         (["g1"], [20, 10, 5]))

    def test_long_orders_get_their_other_lines_a_few_orders_per_request(self):
        shopify_client_chunk = shopify_client.PICKLIST_LINES_CHUNK
        long_ids = [f"L{i}" for i in range(shopify_client_chunk + 1)]
        nodes = [order("short", ("A",))] + [order(gid, ("A",), more=True) for gid in long_ids]

        def answer(query, variables):
            if query is shopify_client.PICKLIST_LINES_BY_ID_QUERY:
                return {"nodes": [{"id": gid, "lineItems": {
                    "pageInfo": {"hasNextPage": gid == "L0", "endCursor": "c2"},
                    "nodes": [line("A"), line("B")]}} for gid in variables["ids"]]}
            return {"order": {"lineItems": {"pageInfo": {"hasNextPage": False}, "nodes": [line("C")]}}}

        self.serve(answer)
        done = shopify_client._complete_line_items(STORE, nodes)
        self.assertEqual(
            ({n["id"]: [li["title"] for li in n["lineItems"]["nodes"]] for n in done},
             [v["ids"] for v in self.queries(shopify_client.PICKLIST_LINES_BY_ID_QUERY)],
             self.queries(shopify_client.PICKLIST_ORDER_LINES_QUERY)),
            ({"short": ["A"], **{gid: ["A", "B", "C"] if gid == "L0" else ["A", "B"] for gid in long_ids}},
             [long_ids[:shopify_client_chunk], long_ids[shopify_client_chunk:]],
             [{"id": "L0", "after": "c2"}]))

    def test_an_order_with_hundreds_of_lines_comes_back_whole(self):
        titles = [f"P{i}" for i in range(230)]
        first = shopify_client.PICKLIST_LINES_PER_ORDER

        def answer(query, variables):
            if query is shopify_client.PICKLIST_LINES_BY_ID_QUERY:
                return {"nodes": [{"id": "big", "lineItems": {"pageInfo": {"hasNextPage": True, "endCursor": "100"},
                                                              "nodes": [line(t) for t in titles[:100]]}}]}
            start = int(variables["after"])
            return {"order": {"lineItems": {
                "pageInfo": {"hasNextPage": start + 100 < len(titles), "endCursor": str(start + 100)},
                "nodes": [line(t) for t in titles[start:start + 100]]}}}

        self.serve(answer)
        [done] = shopify_client._complete_line_items(STORE, [order("big", titles[:first], more=True)])
        self.assertEqual([li["title"] for li in done["lineItems"]["nodes"]], titles)

    def test_changed_orders_are_asked_for_by_update_time_whatever_their_status(self):
        self.serve(lambda query, variables: page([order("g1")]))
        shopify_client.list_changed_picklist_orders(1, datetime(2026, 10, 26, 14, 58, tzinfo=timezone.utc), NOW)
        self.assertEqual([(v["query"], v["sortKey"]) for _, v in self.calls], [(
            "updated_at:>='2026-10-26T14:58:00Z' created_at:>='2026-09-26T00:00:00Z'", "UPDATED_AT")])


if __name__ == "__main__":
    unittest.main()
