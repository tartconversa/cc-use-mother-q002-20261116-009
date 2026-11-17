import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from railwatch.api import create_app
from railwatch.domain import ProductError
from railwatch.report import Reports
from railwatch.service import RailwayService


class ProvenanceTests(unittest.TestCase):
    """铁路新闻证据系统：Republished releases share one origin; independence and divergence stay visible."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = RailwayService(Path(self.temp.name) / "evidence.sqlite3")
        for source_id, host, rank in (("wire", "wire.example.org", 10),
                                      ("gazette", "gazette.example.org", 30),
                                      ("herald", "herald.example.org", 20),
                                      ("times", "times.example.org", 5)):
            self.service.register_source({"source_id": source_id, "host": host,
                                          "kinds": ["freight"], "max_age_days": 30,
                                          "trust_rank": rank, "version": 1, "active": True})
        self.wire_url = "https://wire.example.org/releases/oct"

    def tearDown(self):
        self.temp.cleanup()

    def claim(self, amount="342", metric="cargo_oct"):
        return {"project_key": "rail-a", "kind": "freight", "metric": metric,
                "period": "2026-10-31", "amount": amount, "unit": "tonne",
                "quote": "October freight reached " + amount + " tonnes on the corridor."}

    def capture(self, source_id, external_id, amount="342", metric="cargo_oct",
                host=None, url=None, origin=None):
        host = host or source_id.split("_")[0] + ".example.org"
        claim = self.claim(amount, metric)
        item = {"source_id": source_id, "external_id": external_id,
                "url": url or ("https://" + host + "/news/" + external_id),
                "fetched_at": "2026-11-02T08:00:00Z", "published_at": "2026-11-01T08:00:00Z",
                "title": "Corridor freight " + external_id,
                "body": json.dumps({"text": claim["quote"], "claims": [claim]}),
                "media_type": "application/json"}
        if origin is not None:
            item["origin"] = origin
        return item

    def replay_accept(self, item, expected):
        result = self.service.replay(item)
        self.assertEqual(result["state"], "parsed", result.get("issues"))
        claim_id = result["claim_ids"][0]
        scope = self.service.review(claim_id, {"decision": "accepted", "reviewer": "analyst",
                                               "reason": "Quote verified against captured text",
                                               "expected_version": expected})["scope"]
        return claim_id, scope

    def assert_code(self, code, action, *args):
        with self.assertRaises(ProductError) as caught:
            action(*args)
        self.assertEqual(caught.exception.code, code)

    def chain(self, altered=False):
        """Two republications, one independent report; optionally one republication rewrites the number."""
        wire_amount = "342"
        gazette_amount = "350" if altered else "342"
        wire = self.capture("wire", "rel-oct", wire_amount, url=self.wire_url)
        gazette = self.capture("gazette", "copy-1", gazette_amount, origin={"url": self.wire_url})
        herald = self.capture("herald", "copy-2", "342", origin={"url": self.wire_url})
        times = self.capture("times", "own-1", "342")
        claims, version = [], 0
        for item in (wire, gazette, herald, times):
            claim_id, scope = self.replay_accept(item, version)
            version = scope["version"]
            claims.append(claim_id)
        return claims

    def test_origin_declaration_is_stored(self):
        self.service.replay(self.capture("wire", "rel-oct", url=self.wire_url))
        result = self.service.replay(self.capture("gazette", "copy-1", origin={"url": self.wire_url}))
        row = self.service.capture(result["capture_id"])
        self.assertEqual(row["relation"], "republished")
        self.assertEqual(row["origin_url"], self.wire_url)
        self.assertEqual(row["origin_source_id"], "wire")
        self.assertEqual(row["origin_key"], "url:" + self.wire_url)
        self.assertIsNotNone(row["origin_capture_id"])

    def test_original_capture_marks_itself_as_origin_group(self):
        result = self.service.replay(self.capture("wire", "rel-oct", url=self.wire_url))
        row = self.service.capture(result["capture_id"])
        self.assertEqual(row["relation"], "original")
        self.assertIsNone(row["origin_url"])
        self.assertEqual(row["origin_key"], "url:" + self.wire_url)

    def test_origin_outside_registered_boundary_is_rejected(self):
        item = self.capture("gazette", "copy-1", origin={"url": "https://unregistered.example.net/x"})
        self.assert_code("origin_unregistered", self.service.replay, item)

    def test_origin_self_is_rejected(self):
        item = self.capture("gazette", "copy-1")
        item["origin"] = {"url": item["url"]}
        self.assert_code("origin_self", self.service.replay, item)

    def test_late_original_backfills_republication_link(self):
        republished = self.service.replay(self.capture("gazette", "copy-1", origin={"url": self.wire_url}))
        row = self.service.capture(republished["capture_id"])
        self.assertIsNone(row["origin_capture_id"])
        self.service.replay(self.capture("wire", "rel-oct", url=self.wire_url))
        row = self.service.capture(republished["capture_id"])
        self.assertIsNotNone(row["origin_capture_id"])
        events = [event["event"] for event in self.service.audit()]
        self.assertIn("origin_links_resolved", events)

    def test_republications_do_not_count_as_independent_corroboration(self):
        claims = self.chain()
        comparison = self.service.compare(claims[0])
        self.assertFalse(comparison["conflict"])
        self.assertEqual(len(comparison["groups"]), 1)
        group = comparison["groups"][0]
        self.assertEqual(group["independent_count"], 2)
        self.assertEqual(group["republication_count"], 2)
        origins = {entry["origin_key"]: entry for entry in group["independent_origins"]}
        self.assertEqual(set(origins), {"url:" + self.wire_url,
                                        "url:https://times.example.org/news/own-1"})
        self.assertEqual(sorted(origins["url:" + self.wire_url]["republications"]), claims[1:3])
        self.assertEqual(origins["url:" + self.wire_url]["originals"], [claims[0]])

    def test_selection_prefers_original_over_trusted_republisher(self):
        claims = self.chain()
        scope = self.service.scopes("rail-a")[0]
        self.assertEqual(scope["selected_claim"], claims[0])
        self.assertEqual(scope["selected"]["relation"], "original")
        self.assertEqual(scope["corroboration"]["independent_count"], 2)
        self.assertEqual(scope["corroboration"]["republication_count"], 2)

    def test_report_provenance_explains_origins_and_republications(self):
        self.chain()
        report = Reports(self.service).current("rail-a")
        self.assertEqual(report["selected_count"], 1)
        row = report["provenance"][0]
        self.assertEqual(row["relation"], "original")
        self.assertEqual(row["origin_key"], "url:" + self.wire_url)
        corroboration = row["corroboration"]
        self.assertEqual(corroboration["independent_count"], 2)
        self.assertEqual(corroboration["republication_count"], 2)
        self.assertEqual(corroboration["accepted_count"], 4)

    def test_rewritten_republication_exposes_conflict(self):
        claims = self.chain(altered=True)
        scope = self.service.scopes("rail-a")[0]
        self.assertEqual(scope["conflict"], 1)
        self.assertIsNone(scope["selected"])
        comparison = self.service.compare(claims[0])
        self.assertTrue(comparison["conflict"])
        divergence = comparison["origin_divergence"]
        self.assertEqual(len(divergence), 1)
        self.assertEqual(divergence[0]["origin_key"], "url:" + self.wire_url)
        values = {entry["value"] for entry in divergence[0]["values"]}
        self.assertEqual(values, {"342 tonne", "350 tonne"})
        self.assert_code("unresolved_scope", self.service.publish, {"name": "release-x"})
        report = Reports(self.service).current("rail-a")
        self.assertEqual(report["unresolved_count"], 1)
        entry = report["unresolved"][0]
        self.assertTrue(entry["conflict"])
        self.assertEqual({v["value"] for v in entry["origin_divergence"][0]["values"]},
                         {"342 tonne", "350 tonne"})

    def test_snapshot_freezes_provenance_despite_later_correction(self):
        claims = self.chain()
        released = self.service.publish({"name": "release-1", "project_key": "rail-a"})
        before = self.service.snapshot(released["snapshot_id"])
        before_report = Reports(self.service).snapshot(released["snapshot_id"], "rail-a")
        times_capture = self.service.claim(claims[3])["capture_id"]
        corrected = self.service.correct_origin(times_capture, {"origin": {"url": self.wire_url}})
        self.assertEqual(corrected["relation"], "republished")
        self.assertEqual(corrected["origin_source_id"], "wire")
        after = self.service.snapshot(released["snapshot_id"])
        after_report = Reports(self.service).snapshot(released["snapshot_id"], "rail-a")
        self.assertEqual(before["digest"], after["digest"])
        self.assertEqual(before["content"], after["content"])
        self.assertEqual(before_report, after_report)
        live = self.service.scopes("rail-a")[0]
        self.assertEqual(live["corroboration"]["independent_count"], 1)
        self.assertEqual(live["corroboration"]["republication_count"], 3)
        events = [event["event"] for event in self.service.audit()]
        self.assertIn("capture_origin_corrected", events)

    def test_correction_can_clear_origin(self):
        result = self.service.replay(self.capture("gazette", "copy-1", origin={"url": self.wire_url}))
        corrected = self.service.correct_origin(result["capture_id"], {"origin": None})
        self.assertEqual(corrected["relation"], "original")
        self.assertIsNone(corrected["origin_url"])

    def test_correction_keeps_boundary(self):
        result = self.service.replay(self.capture("gazette", "copy-1"))
        self.assert_code("origin_unregistered", self.service.correct_origin,
                         result["capture_id"], {"origin": {"url": "https://elsewhere.example.net/a"}})


class ProvenanceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.temp.name) / "api.sqlite3"))
        for source_id, host in (("wire", "wire.example.org"), ("gazette", "gazette.example.org")):
            self.client.put(f"/sources/{source_id}", json={"source_id": source_id, "host": host,
                                                           "kinds": ["freight"], "max_age_days": 30,
                                                           "trust_rank": 10, "version": 1, "active": True})

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def capture(self, source_id, external_id, origin=None):
        claim = {"project_key": "rail-a", "kind": "freight", "metric": "cargo",
                 "period": "2026-10-31", "amount": "342", "unit": "tonne",
                 "quote": "October freight reached 342 tonnes on the corridor."}
        item = {"source_id": source_id, "external_id": external_id,
                "url": "https://" + source_id + ".example.org/news/" + external_id,
                "fetched_at": "2026-11-02T08:00:00Z", "published_at": "2026-11-01T08:00:00Z",
                "title": "Corridor freight", "media_type": "application/json",
                "body": json.dumps({"text": claim["quote"], "claims": [claim]})}
        if origin is not None:
            item["origin"] = origin
        return item

    def test_capture_with_origin_roundtrip(self):
        self.client.post("/captures", json=self.capture("wire", "rel-1"))
        origin = {"url": "https://wire.example.org/news/rel-1"}
        response = self.client.post("/captures", json=self.capture("gazette", "copy-1", origin))
        self.assertEqual(response.status_code, 201)
        row = self.client.get(f"/captures/{response.json()['capture_id']}").json()
        self.assertEqual(row["relation"], "republished")
        self.assertEqual(row["origin_source_id"], "wire")

    def test_unregistered_origin_rejected_via_api(self):
        response = self.client.post("/captures", json=self.capture("gazette", "copy-1",
                                    {"url": "https://stranger.example.net/a"}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "origin_unregistered")

    def test_origin_correction_endpoint(self):
        self.client.post("/captures", json=self.capture("wire", "rel-1"))
        capture_id = self.client.post("/captures", json=self.capture("gazette", "copy-1")).json()["capture_id"]
        response = self.client.post(f"/captures/{capture_id}/origin",
                                    json={"origin": {"url": "https://wire.example.org/news/rel-1"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["relation"], "republished")
        self.assertEqual(response.json()["origin_url"], "https://wire.example.org/news/rel-1")

    def test_comparison_endpoint_exposes_origin_structure(self):
        self.client.post("/captures", json=self.capture("wire", "rel-1"))
        origin = {"url": "https://wire.example.org/news/rel-1"}
        self.client.post("/captures", json=self.capture("gazette", "copy-1", origin))
        for claim in self.client.get("/claims").json():
            self.client.post(f"/claims/{claim['id']}/reviews",
                             json={"decision": "accepted", "reviewer": "analyst",
                                   "reason": "Verified", "expected_version": claim["id"] - 1})
        comparison = self.client.get("/claims/1/comparison").json()
        self.assertEqual(comparison["groups"][0]["independent_count"], 1)
        self.assertEqual(comparison["groups"][0]["republication_count"], 1)


if __name__ == "__main__":
    unittest.main()
