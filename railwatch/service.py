"""铁路新闻证据系统：Coordinates replay, extraction, review and reproducible evidence releases."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from urllib.parse import urlsplit

from .domain import Capture, Claim, Origin, ProductError, SourcePolicy, fingerprint, token
from .extract import Extractor
from .schedule import Schedule
from .store import Store


class RailwayService:
    def __init__(self, db_path):
        self.store = Store(db_path)
        self.schedule = Schedule(self.store)
        self.extractor = Extractor()

    def register_source(self, data, expected_version=None):
        policy = SourcePolicy.parse(data)
        return self.store.upsert_source(policy, expected_version)

    def policy(self, source_id):
        row = self.store.source(source_id)
        return SourcePolicy(row["source_id"], row["host"], tuple(row["kinds"]),
                            row["max_age_days"], row["trust_rank"], row["version"], row["active"])

    def replay(self, data):
        capture = Capture.parse(data)
        policy = self.policy(capture.source_id)
        capture.check(policy)
        provenance = self._provenance(capture.url, capture.origin)
        existing = self.store.capture_identity(capture.source_id, capture.external_id)
        same = next((row for row in existing if row["content_hash"] == capture.content_hash()), None)
        if same:
            return {"capture_id": same["id"], "created": False, "state": same["state"],
                    "claim_ids": [c["id"] for c in self.store.claims_for_capture(same["id"])], "issues": []}
        claims, issues = self.extractor.inspect(capture, policy)
        capture_id, created = self.store.put_capture(capture, policy, provenance)
        if issues and not claims:
            self.store.set_capture_state(capture_id, "received", "quarantined", {"issues": issues})
            return {"capture_id": capture_id, "created": created, "state": "quarantined", "claim_ids": [], "issues": issues}
        if issues:
            self.store.set_capture_state(capture_id, "received", "quarantined", {"issues": issues})
            return {"capture_id": capture_id, "created": created, "state": "quarantined", "claim_ids": [], "issues": issues}
        claim_ids = self.store.insert_claims(capture_id, claims)
        return {"capture_id": capture_id, "created": created, "state": "parsed", "claim_ids": claim_ids, "issues": []}

    def _provenance(self, own_url, origin):
        """铁路新闻证据系统：Resolve a declared origin inside the registered crawl boundary."""
        if origin is None:
            return {"relation": "original", "origin_url": None, "origin_source_id": None,
                    "origin_capture_id": None, "origin_key": "url:" + own_url}
        if origin.url == own_url:
            raise ProductError("origin_self", "declared origin must differ from the capture URL")
        host = urlsplit(origin.url).hostname
        source = self.store.source_by_host(host)
        if source is None:
            raise ProductError("origin_unregistered",
                               "declared origin is outside the registered source boundary")
        original = self.store.capture_by_url(source["source_id"], origin.url)
        return {"relation": "republished", "origin_url": origin.url,
                "origin_source_id": source["source_id"],
                "origin_capture_id": original["id"] if original else None,
                "origin_key": "url:" + origin.url}

    def correct_origin(self, capture_id, data):
        row = self.store.capture(capture_id)
        origin = Origin.parse(data.get("origin") if isinstance(data, dict) else None)
        provenance = self._provenance(row["url"], origin)
        return self.store.update_origin(capture_id, provenance)

    def capture(self, capture_id):
        row = self.store.capture(capture_id)
        row["claims"] = self.store.claims_for_capture(capture_id)
        return row

    def review_queue(self, project_key=None, state="proposed", limit=100):
        if state not in {"proposed", "accepted", "rejected", "hold", "retracted"}:
            raise ProductError("invalid_state", "unsupported review state")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ProductError("invalid_limit", "limit must be 1..500")
        if project_key:
            project_key = token(project_key, "project_key")
            return self.store.all("SELECT c.*,a.source_id,a.url,a.published_at FROM claims c JOIN captures a ON a.id=c.capture_id WHERE c.state=? AND c.project_key=? ORDER BY c.id LIMIT ?",
                                  (state, project_key, limit))
        return self.store.all("SELECT c.*,a.source_id,a.url,a.published_at FROM claims c JOIN captures a ON a.id=c.capture_id WHERE c.state=? ORDER BY c.id LIMIT ?",
                              (state, limit))

    def claim(self, claim_id):
        row = self.store.claim(claim_id)
        row["capture"] = self.store.capture(row["capture_id"])
        row["scope"] = self.store.scope(self._key(row))
        return row

    @staticmethod
    def _key(row):
        return (row["project_key"], row["kind"], row["metric"], row["period"])

    @staticmethod
    def _same(a, b):
        if a["kind"] == "milestone":
            return a["milestone"] == b["milestone"]
        return a["unit"] == b["unit"] and Decimal(a["amount"]) == Decimal(b["amount"])

    @staticmethod
    def _relation(row):
        return row.get("relation") or "original"

    @staticmethod
    def _origin_key_of(row):
        return row.get("origin_key") or ("url:" + row["capture_url"])

    @staticmethod
    def _value_label(row):
        if row["kind"] == "milestone":
            return row["milestone"]
        return f"{row['amount']} {row['unit']}"

    def _origin_structure(self, claims):
        """铁路新闻证据系统：Group accepted claims into independent origins and their republications."""
        origins = {}
        for claim in claims:
            key = self._origin_key_of(claim)
            entry = origins.setdefault(key, {"origin_key": key, "origin_source_id": claim.get("origin_source_id"),
                                             "sources": set(), "originals": [], "republications": []})
            entry["sources"].add(claim["source_id"])
            slot = "republications" if self._relation(claim) == "republished" else "originals"
            entry[slot].append(claim["id"])
            if claim.get("origin_source_id") and not entry["origin_source_id"]:
                entry["origin_source_id"] = claim["origin_source_id"]
        result = []
        for entry in origins.values():
            entry["sources"] = sorted(entry["sources"])
            entry["originals"].sort()
            entry["republications"].sort()
            result.append(entry)
        result.sort(key=lambda entry: entry["origin_key"])
        return result

    @staticmethod
    def _preferred(group):
        return sorted(group, key=lambda c: (c.get("relation") == "republished", -c["trust_rank"], c["id"]))[0]

    def _group_detail(self, group):
        origins = self._origin_structure(group)
        republications = sum(len(entry["republications"]) for entry in origins)
        return {"claim_ids": [c["id"] for c in group], "value": self._value_label(group[0]),
                "independent_count": len(origins), "republication_count": republications,
                "independent_origins": origins}

    def _origin_divergence(self, groups):
        """铁路新闻证据系统：Expose origins whose republications disagree on value; never fold them away."""
        by_origin = {}
        for group in groups:
            value = self._value_label(group[0])
            for claim in group:
                entry = by_origin.setdefault(self._origin_key_of(claim),
                                             {"origin_key": self._origin_key_of(claim),
                                              "origin_source_id": claim.get("origin_source_id"), "values": {}})
                entry["values"].setdefault(value, []).append(claim["id"])
                if claim.get("origin_source_id") and not entry["origin_source_id"]:
                    entry["origin_source_id"] = claim["origin_source_id"]
        divergence = []
        for entry in by_origin.values():
            if len(entry["values"]) > 1:
                divergence.append({"origin_key": entry["origin_key"],
                                   "origin_source_id": entry["origin_source_id"],
                                   "values": [{"value": value, "claim_ids": sorted(ids)}
                                              for value, ids in sorted(entry["values"].items())]})
        divergence.sort(key=lambda entry: entry["origin_key"])
        return divergence

    def _accepted_groups(self, key):
        candidates = self.store.candidates(key)
        accepted = [c for c in candidates if c["state"] == "accepted" and c["capture_state"] == "parsed"]
        groups = []
        for candidate in accepted:
            group = next((g for g in groups if self._same(g[0], candidate)), None)
            if group is None:
                groups.append([candidate])
            else:
                group.append(candidate)
        return candidates, accepted, groups

    def divergence(self, key):
        """铁路新闻证据系统：Same-origin value divergence for one scope key, for report exposure."""
        _, _, groups = self._accepted_groups(key)
        return self._origin_divergence(groups)

    def compare(self, claim_id):
        claim = self.store.claim(claim_id)
        key = self._key(claim)
        scope = self.store.scope(key)
        candidates, accepted, groups = self._accepted_groups(key)
        groups.sort(key=lambda group: (-min(c["trust_rank"] for c in group), -len(group), min(c["id"] for c in group)))
        return {"key": key, "scope": scope, "candidate_count": len(candidates),
                "accepted_count": len(accepted), "value_groups": [[c["id"] for c in group] for group in groups],
                "groups": [self._group_detail(group) for group in groups],
                "conflict": len(groups) > 1,
                "origin_divergence": self._origin_divergence(groups),
                "suggested_claim": self._preferred(groups[0])["id"] if groups else None}

    def review(self, claim_id, data):
        claim = self.store.claim(claim_id)
        if claim["state"] != "proposed":
            raise ProductError("already_reviewed", "claim has already been reviewed", 409)
        decision = data.get("decision")
        if decision not in {"accepted", "rejected", "hold"}:
            raise ProductError("invalid_decision", "decision must be accepted, rejected or hold")
        reviewer = token(data.get("reviewer"), "reviewer")
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ProductError("invalid_reason", "review requires a short reason")
        expected = data.get("expected_version")
        if type(expected) is not int or expected < 0:
            raise ProductError("invalid_version", "expected_version must be nonnegative")
        capture = self.store.capture(claim["capture_id"])
        if capture["state"] != "parsed":
            raise ProductError("capture_state", "claim capture is not parsed", 409)
        key = self._key(claim)
        candidates = self.store.candidates(key)
        accepted = [c for c in candidates if c["state"] == "accepted" and c["capture_state"] == "parsed"]
        if decision == "accepted":
            accepted.append({**claim, "trust_rank": self.store.source(capture["source_id"])["trust_rank"],
                             "relation": capture.get("relation") or "original"})
        selected, conflict = self._selection(accepted)
        scope = self.store.record_review(claim_id, decision, reviewer, reason.strip(), expected, selected, conflict)
        return {"claim_id": claim_id, "decision": decision, "scope": scope}

    def _selection(self, accepted):
        if not accepted:
            return None, False
        groups = []
        for candidate in accepted:
            group = next((g for g in groups if self._same(g[0], candidate)), None)
            if group is None:
                groups.append([candidate])
            else:
                group.append(candidate)
        if len(groups) > 1:
            return None, True
        return self._preferred(groups[0])["id"], False

    def scopes(self, project_key=None, include_empty=True):
        if project_key:
            project_key = token(project_key, "project_key")
            rows = self.store.all("SELECT * FROM scopes WHERE project_key=? ORDER BY kind,metric,period", (project_key,))
        else:
            rows = self.store.all("SELECT * FROM scopes ORDER BY project_key,kind,metric,period")
        for row in rows:
            if row["selected_claim"]:
                claim = self.store.claim(row["selected_claim"])
                capture = self.store.capture(claim["capture_id"])
                row["selected"] = {"claim_id": claim["id"], "amount": claim["amount"],
                                   "unit": claim["unit"], "milestone": claim["milestone"],
                                   "quote": claim["quote"], "source_id": capture["source_id"],
                                   "url": capture["url"], "published_at": capture["published_at"],
                                   "relation": capture.get("relation") or "original",
                                   "origin_url": capture.get("origin_url"),
                                   "origin_source_id": capture.get("origin_source_id"),
                                   "origin_key": capture.get("origin_key") or ("url:" + capture["url"])}
                row["corroboration"] = self._corroboration(row)
            else:
                row["selected"] = None
                row["corroboration"] = None
        return rows if include_empty else [row for row in rows if row["selected"]]

    def _corroboration(self, scope):
        """铁路新闻证据系统：Explain which independent origins and republications back the selected value."""
        key = (scope["project_key"], scope["kind"], scope["metric"], scope["period"])
        selected = {**scope["selected"], "kind": scope["kind"]}
        candidates = self.store.candidates(key)
        accepted = [c for c in candidates if c["state"] == "accepted" and c["capture_state"] == "parsed"]
        agreeing = [c for c in accepted if self._same(c, selected)]
        origins = self._origin_structure(agreeing)
        republications = sum(len(entry["republications"]) for entry in origins)
        return {"independent_count": len(origins), "republication_count": republications,
                "accepted_count": len(agreeing), "independent_origins": origins}

    def publish(self, data):
        name = token(data.get("name"), "name")
        project_key = data.get("project_key")
        scopes = self.scopes(project_key)
        if not scopes:
            raise ProductError("empty_release", "no reviewed scopes to publish", 409)
        blocked = [row for row in scopes if row["conflict"] or row["selected"] is None]
        if blocked:
            raise ProductError("unresolved_scope", f"{len(blocked)} scopes are unresolved", 409)
        row, created = self.store.save_snapshot(name, scopes)
        return {"snapshot_id": row["id"], "digest": row["digest"], "created": created,
                "scope_count": row["scope_count"], "claim_count": row["claim_count"]}

    def snapshot(self, snapshot_id):
        return self.store.snapshot(snapshot_id)

    def audit(self, since=0, limit=100):
        if type(since) is not int or since < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ProductError("invalid_page", "invalid audit pagination")
        return self.store.audit_events(since, limit)
