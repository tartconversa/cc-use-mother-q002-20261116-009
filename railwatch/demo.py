"""铁路新闻证据系统：Offline, deterministic walkthrough of the evidence publication workflow.

本地样本链路：wire 发布原始通稿，gazette 与 herald 转载同一通稿，times 独立报道同一指标；
另有一条 herald 改数的转载制造分歧。演示验证独立出处与同源转载的不同佐证含义、
分歧的显式暴露、快照冻结后的归因更正不影响历史快照。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .domain import ProductError
from .quality import Quality
from .report import Reports
from .service import RailwayService

WIRE_URL = "https://wire.example.org/releases/oct"


def sources():
    return [("wire", "wire.example.org", 10), ("gazette", "gazette.example.org", 30),
            ("herald", "herald.example.org", 20), ("times", "times.example.org", 5)]


def article(source_id, external_id, amount, metric, project="route_demo", url=None, origin=None):
    quote = f"October freight reached {amount} tonnes on the corridor."
    claim = {"project_key": project, "kind": "freight", "metric": metric,
             "period": "2026-10-31", "amount": amount, "unit": "tonne", "quote": quote}
    item = {"source_id": source_id, "external_id": external_id,
            "url": url or f"https://{source_id}.example.org/news/{external_id}",
            "fetched_at": "2026-11-02T08:00:00Z", "published_at": "2026-11-01T08:00:00Z",
            "title": f"Corridor freight {external_id}",
            "body": json.dumps({"text": quote, "claims": [claim]}),
            "media_type": "application/json", "metadata": {"origin": "local-synthetic"}}
    if origin is not None:
        item["origin"] = origin
    return item


def accept(service, item, version):
    result = service.replay(item)
    assert result["state"] == "parsed", result.get("issues")
    claim_id = result["claim_ids"][0]
    scope = service.review(claim_id, {"decision": "accepted", "reviewer": "demo_reviewer",
                                      "reason": "Quote verified against captured text",
                                      "expected_version": version})["scope"]
    return result["capture_id"], claim_id, scope


def main():
    with tempfile.TemporaryDirectory() as temporary:
        service = RailwayService(Path(temporary) / "rail.sqlite3")
        for source_id, host, rank in sources():
            service.register_source({"source_id": source_id, "host": host, "kinds": ["freight"],
                                     "max_age_days": 30, "trust_rank": rank, "version": 1, "active": True})

        # 两站点转载同一通稿，第三站点独立报道，数值一致。
        captures, claims, version = {}, {}, 0
        for key, item in [("wire", article("wire", "rel-oct", "342", "cargo_oct", url=WIRE_URL)),
                          ("gazette", article("gazette", "copy-1", "342", "cargo_oct",
                                              origin={"url": WIRE_URL})),
                          ("herald", article("herald", "copy-2", "342", "cargo_oct",
                                             origin={"url": WIRE_URL})),
                          ("times", article("times", "own-1", "342", "cargo_oct"))]:
            captures[key], claims[key], scope = accept(service, item, version)
            version = scope["version"]
        oct_scope = scope
        if oct_scope["selected_claim"] != claims["wire"]:
            raise AssertionError("original release should be selected over republications")
        comparison = service.compare(claims["wire"])
        group = comparison["groups"][0]
        if group["independent_count"] != 2 or group["republication_count"] != 2:
            raise AssertionError("republications must not count as independent corroboration")

        # 转载站点改写数字：同一出处出现分歧，必须显式暴露且阻断其项目发布。
        sep_wire = article("wire", "rel-sep", "300", "cargo_sep", project="route_demo_sep",
                           url="https://wire.example.org/releases/sep")
        sep_herald = article("herald", "copy-sep", "303", "cargo_sep", project="route_demo_sep",
                             origin={"url": "https://wire.example.org/releases/sep"})
        _, sep_wire_claim, sep_scope = accept(service, sep_wire, 0)
        _, _, sep_scope = accept(service, sep_herald, sep_scope["version"])
        if not sep_scope["conflict"]:
            raise AssertionError("rewritten republication must raise a conflict")
        divergence = service.compare(sep_wire_claim)["origin_divergence"]
        exposed = {value["value"] for entry in divergence for value in entry["values"]}
        if exposed != {"300 tonne", "303 tonne"}:
            raise AssertionError("origin divergence must expose both values explicitly")
        try:
            service.publish({"name": "route_demo_sep_blocked", "project_key": "route_demo_sep"})
            raise AssertionError("publish must stay blocked while a divergence is unresolved")
        except ProductError as error:
            if error.code != "unresolved_scope":
                raise
        report = Reports(service).current("route_demo_sep")
        flagged = [entry for entry in report["unresolved"] if entry.get("origin_divergence")]
        if len(flagged) != 1:
            raise AssertionError("current report must surface the divergent origin")

        # 一致链路无冲突，发布冻结快照。
        released = service.publish({"name": "route_demo_release", "project_key": "route_demo"})
        snapshot_report = Reports(service).snapshot(released["snapshot_id"], "route_demo")
        oct_row = next(row for row in snapshot_report["provenance"] if row["period"] == "2026-10-31"
                       and row["corroboration"]["independent_count"] == 2)
        if oct_row["relation"] != "original" or oct_row["corroboration"]["republication_count"] != 2:
            raise AssertionError("snapshot provenance must name independent origins and republications")
        quality = Quality(service).publication_readiness("route_demo")
        if quality["ready"] is not True:
            raise AssertionError("reviewed route must be publishable")

        # 日后更正归因：times 被确认也是转载。当前报告反映新归因，历史快照不变。
        service.correct_origin(captures["times"], {"origin": {"url": WIRE_URL}})
        corrected = Reports(service).current("route_demo")
        corrected_oct = next(row for row in corrected["provenance"] if row["claim_id"] == claims["wire"])
        if corrected_oct["corroboration"]["independent_count"] != 1:
            raise AssertionError("current report must reflect the corrected attribution")
        frozen = Reports(service).snapshot(released["snapshot_id"], "route_demo")
        frozen_oct = next(row for row in frozen["provenance"] if row["claim_id"] == claims["wire"])
        if frozen_oct["corroboration"]["independent_count"] != 2:
            raise AssertionError("frozen snapshot must not change after attribution correction")
        if frozen["snapshot_digest"] != released["digest"]:
            raise AssertionError("snapshot digest must survive later corrections")

        print(json.dumps({"capture_id": captures["wire"], "claim_id": claims["wire"],
                          "scope_version": oct_scope["version"],
                          "snapshot_id": released["snapshot_id"], "digest": released["digest"],
                          "latest_tonnes": snapshot_report["series"][0]["latest"],
                          "provenance_count": len(snapshot_report["provenance"]),
                          "independent_origins": oct_row["corroboration"]["independent_count"],
                          "republications": oct_row["corroboration"]["republication_count"],
                          "divergence_exposed": sorted(exposed),
                          "corrected_independent_origins":
                              corrected_oct["corroboration"]["independent_count"],
                          "snapshot_frozen": True, "ready": quality["ready"]},
                         ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
