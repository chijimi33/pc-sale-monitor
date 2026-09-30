from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from sale_monitor.models import iso
from sale_monitor.storage import Store
from sale_monitor.reporting import aggregate
from sale_monitor.validation_integrity import audit_counts, capture_provenance, scheduled_samples, verified_provenance, REPOSITORY

NOW = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)


def provenance(key, event="schedule", created=None, attempt=1):
    return {"source": "github_actions_api", "repository": REPOSITORY, "run_id": str(key), "event": event,
            "created_at": iso(created or NOW), "max_attempt": attempt, "checked_at": iso(NOW),
            "workflow_path": ".github/workflows/monitor.yml",
            "api_url": f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{key}"}


def audit():
    return {"reviewed_at": iso(NOW), "reviewer": "Codex", "reviewed_count": 20, "needs_review_count": 0, "false_positive_count": 0,
            "audits": [{"event_id": f"event-{i}", "offer_key": f"item-{i}", "source_run_id": "100-1", "reviewed_at": iso(NOW),
                        "status": "passed", "findings": [], "price_check": {"status": "supported", "rule": "A"},
                        "evidence": [{"store": "ark", "url": "https://www.ark-pc.co.jp/i/123/", "observed_at": iso(NOW), "body_sha256": "a"*64}]} for i in range(20)]}


class ValidationIntegrityTests(unittest.TestCase):
    def test_push_manual_and_unknown_do_not_supply_schedule_windows(self):
        snapshots=[];catalog={"runs": {}}
        for i in range(40):
            key=str(100+i);when=NOW-timedelta(hours=4*i)
            snapshots.append({"run_id": key+"-1", "generated_at": iso(when)})
            catalog["runs"][key]=provenance(key, "push" if i % 2 else "workflow_dispatch", when)
        snapshots.append({"run_id": "999-1", "generated_at": iso(NOW)})
        sampled, counts=scheduled_samples(snapshots,catalog,NOW)
        self.assertEqual(sampled,[])
        self.assertEqual(counts["event_counts"],{"push":20,"workflow_dispatch":20,"unknown":1})
        self.assertEqual(counts["unknown_run_ids"],["999-1"])

    def test_retry_attempt_uses_original_creation_slot_and_latest_observation(self):
        first=NOW-timedelta(days=1)
        catalog={"runs":{"123":provenance("123",created=first,attempt=2),"124":provenance("124",created=first+timedelta(minutes=1))}}
        one={"run_id":"123-1","generated_at":iso(first+timedelta(hours=1))}
        retry={"run_id":"123-2","generated_at":iso(NOW)}
        other={"run_id":"124-1","generated_at":iso(first+timedelta(hours=1))}
        sampled,counts=scheduled_samples([one,retry,other],catalog,NOW)
        self.assertEqual(sampled,[retry])
        self.assertEqual(counts["unique_scheduled_runs"],2)
        catalog["runs"]["123"]["created_at"]=iso(NOW-timedelta(days=8))
        self.assertEqual(scheduled_samples([retry],catalog,NOW)[0],[])

    def test_invalid_provenance_is_unknown_not_a_guessed_schedule(self):
        catalog={"runs":{"123":provenance("123")}}
        for field,bad in [("repository","someone/else"),("source","timestamp_guess"),("workflow_path",".github/workflows/tests.yml"),("api_url","https://invalid.example"),("max_attempt",True),("created_at","invalid")]:
            test=deepcopy(catalog);test["runs"]["123"][field]=bad
            self.assertIsNone(verified_provenance("123-1",test),field)
        self.assertIsNone(verified_provenance("123-2",catalog))
        self.assertIsNone(verified_provenance("fake",catalog))

    def test_api_backfill_is_paginated_cached_and_failure_preserves_records(self):
        with tempfile.TemporaryDirectory() as folder:
            disk=Store(Path(folder));disk.save("metrics/123-1.json",{})
            urls=[]
            api={"id":123,"run_attempt":1,"repository":{"full_name":REPOSITORY},"event":"schedule","created_at":iso(NOW),"path":".github/workflows/monitor.yml","head_sha":"a"*40}
            def fetch(url):
                urls.append(url)
                return {"workflow_runs":[{"id":1}]*100 if len(urls)==1 else [api]}
            status=capture_provenance(Path(folder),"123-1",fetch=fetch,now=NOW)
            self.assertEqual(status["api_requests"],2)
            self.assertEqual(status["unresolved_run_ids"],[])
            catalog=disk.load("validation/run_provenance.json",{})
            self.assertEqual(verified_provenance("123-1",catalog)["event"],"schedule")
            self.assertEqual(capture_provenance(Path(folder),"123-1",fetch=fetch,now=NOW)["api_requests"],0)
            def fail(url): raise OSError("temporary")
            status=capture_provenance(Path(folder),"124-1",fetch=fail,now=NOW)
            self.assertEqual(status["errors"],["OSError"])
            self.assertEqual(status["unresolved_run_ids"],["124-1"])
            self.assertEqual(disk.load("validation/run_provenance.json",{})["runs"],catalog["runs"])

    def test_aggregation_publishes_verified_provenance_without_creating_audits(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);disk=Store(root)
            disk.save("validation/run_provenance.json",{"runs":{"123":provenance("123")}})
            index=aggregate(root,root/"public","123-1",NOW)
            self.assertEqual(index["run_provenance"]["event"],"schedule")
            self.assertEqual(index["run_provenance"]["api_url"],f"https://api.github.com/repos/{REPOSITORY}/actions/runs/123")
            report=disk.load("public/validation.json",{})
            self.assertEqual(report["measured_four_hour_windows"],1)
            self.assertEqual(report["manual_review_verified"]["reviewed_count"],0)
            self.assertFalse(report["cutover_ready"])
            self.assertFalse((root/"validation/manual_review.json").exists())

    def test_counts_require_twenty_distinct_final_records_and_preserve_input(self):
        good=audit();original=deepcopy(good)
        self.assertEqual(audit_counts(good,NOW)["reviewed_count"],20)
        good["audits"]=[]
        self.assertEqual(audit_counts(good,NOW)["reviewed_count"],0)
        good["audits"]=[deepcopy(original["audits"][0]) for _ in range(20)]
        result=audit_counts(good,NOW)
        self.assertEqual(result["reviewed_count"],0)
        self.assertEqual(result["duplicate_event_ids"],["event-0"])
        check=deepcopy(original);audit_counts(check,NOW)
        self.assertEqual(check,original)

    def test_proposals_missing_evidence_and_future_reviews_cannot_pass(self):
        cases=[("proposal_only",True),("audit_status","proposal_only"),("reviewer","Qwen (proposal)"),("reviewed_at",iso(NOW+timedelta(seconds=1))),("evidence",[]),("price_check",{})]
        for key,value in cases:
            value_audit=audit();value_audit["audits"][0][key]=value
            result=audit_counts(value_audit,NOW)
            self.assertEqual(result["reviewed_count"],19,key)
            self.assertTrue(result["invalid_records"],key)
        value_audit=audit();value_audit["mode"]="proposal_only"
        self.assertEqual(audit_counts(value_audit,NOW)["reviewed_count"],0)
        for url in ["https://[invalid","https://item.rakuten.co.jp/test/item"]:
            value_audit=audit();value_audit["audits"][0]["evidence"][0]["url"]=url
            self.assertEqual(audit_counts(value_audit,NOW)["reviewed_count"],19)

    def test_unresolved_and_false_positive_cannot_hide_in_declared_zero(self):
        value=audit();value["audits"][0]["findings"]=[{"status":"open"}]
        value["audits"][1]["status"]="false_positive"
        result=audit_counts(value,NOW)
        self.assertEqual(result["reviewed_count"],18)
        self.assertEqual(result["needs_review_count"],1)
        self.assertEqual(result["false_positive_count"],1)
        self.assertEqual(set(result["declared_count_mismatches"]),{"reviewed_count","needs_review_count","false_positive_count"})


if __name__ == "__main__":
    unittest.main()
