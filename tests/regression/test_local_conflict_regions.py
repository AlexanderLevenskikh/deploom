"""Local scheduling hints cannot change physical verification authority."""
import json
import unittest
from cohort_conflict_regions import update_regions
from cohort_attempt_telemetry import append_attempt
from iterative_cohort_planner import plan_adaptive_cohort
from baseline_constraint_verifier import assignment_fingerprint as fp

class LocalRegionTests(unittest.TestCase):
    config={"cohortMaxPackages":24,"cohortSchedulingStrategy":"local-adaptive-region"}
    names=[f"p{i:02}" for i in range(72)]
    def event(self,regions,names,outcome="FAIL",evidence="opaque",source="s",accepted=None):
        return update_regions(regions,scope="run",config=self.config,
            candidate={"candidateId":"candidate","regionSourceKey":source,"delta":{"changed":dict.fromkeys(names,"2.0.0")}},
            outcome=outcome,stage="tsc",evidence=evidence,accepted_delta=accepted)
    def plan(self,regions,old=None,atoms=(),nogoods=(),blocks=(),ledger=None):
        old=old or dict.fromkeys(self.names,"1.0.0")
        return plan_adaptive_cohort(incumbent=old,desired=dict.fromkeys(self.names,"2.0.0"),
            config={**self.config,"cohortAtoms":atoms},ledger=ledger or {"conflictRegions":regions},checkpoints=[],
            base_checkpoint_id="C0",blocked_fingerprints=blocks,learned_nogoods=nogoods,fingerprint_fn=fp)
    def test_opaque_failure_shrinks_only_failed_region_and_switches_large(self):
        regions,r=self.event([],self.names[:24])
        self.assertEqual(r["localSoftSize"],12)
        plan,detail=self.plan(regions)
        self.assertEqual(len(plan.packages),24)
        self.assertFalse(set(plan.packages)&set(self.names[:24]))
        self.assertEqual(detail["operatorReason"],"independent-intact-cohort")
    def test_structural_failure_does_not_shrink_and_uses_exact_clause(self):
        regions,r=self.event([],self.names[:24],evidence="structural")
        self.assertEqual(r["localSoftSize"],24)
        plan,_=self.plan(regions,nogoods=({"p00":"2.0.0","p01":"2.0.0"},))
        self.assertFalse(set(("p00","p01")).issubset(plan.packages))
    def test_unknown_preserves_regions_and_sizes(self):
        regions,r=self.event([],self.names[:24]);before=json.dumps(regions,sort_keys=True)
        for _ in range(3):regions,_=self.event(regions,self.names[:12],outcome="UNKNOWN")
        self.assertEqual(json.dumps(regions,sort_keys=True),before)
        self.assertEqual(self.event([],self.names,outcome="UNKNOWN")[0],[])
    def test_multiple_regions_keep_independent_sizes(self):
        regions,a=self.event([],self.names[:24]);regions,b=self.event(regions,self.names[24:48])
        regions,a=self.event(regions,self.names[:12])
        self.assertEqual([r["localSoftSize"] for r in regions],[12,12,6])
        self.assertEqual(len({r["regionId"] for r in regions}),3)
        plan,_=self.plan(regions);self.assertEqual(plan.packages,tuple(self.names[48:72]))
    def test_restart_preserves_id_and_progress_only_on_acceptance(self):
        regions,r=self.event([],self.names[:24]);rid=r["regionId"]
        regions,_=self.event(json.loads(json.dumps(regions)),self.names[:12],outcome="PASS")
        self.assertIsNone(regions[0]["lastProgress"])
        regions,r=self.event(regions,self.names[:12],outcome="PASS",accepted={"changed":{}})
        self.assertEqual(r["regionId"],rid);self.assertEqual(r["attemptsWithoutProgress"],0)
    def test_overlapping_failures_merge_with_lineage(self):
        regions,a=self.event([],self.names[:12]);regions,b=self.event(regions,self.names[24:36])
        parents={a["regionId"],b["regionId"]}
        regions,r=self.event(regions,["p00","p24"])
        self.assertEqual(set(r["lineage"]),parents);self.assertEqual(len(regions),3)
        self.assertEqual(r["packages"],["p00","p24"])
    def test_new_source_does_not_update_old_failure(self):
        regions,a=self.event([],self.names[:24]);regions,b=self.event(regions,self.names[:12],source="repaired")
        self.assertEqual(len(regions),2);self.assertEqual(a["localSoftSize"],12)
        self.assertNotEqual(a["regionId"],b["regionId"])
    def test_atom_and_companion_do_not_break_local_soft_size(self):
        regions,_=self.event([],self.names[:24])
        for _ in range(4):regions,_=self.event(regions,self.names[:3])
        old={**dict.fromkeys(self.names,"2.0.0"),**dict.fromkeys(self.names[:3],"1.0.0")}
        plan,detail=self.plan(regions,old=old,atoms=[self.names[:3]])
        self.assertEqual(plan.packages,tuple(self.names[:3]))
        self.assertEqual(detail["selectedAtoms"],[self.names[:3]])
        self.assertEqual(detail["operator"],"local-split")
    def test_accepted_subset_leaves_remainder_and_large_unrelated(self):
        regions,_=self.event([],self.names[:24]);regions,_=self.event(regions,self.names[:12],outcome="PASS",accepted={"changed":{}})
        old=dict.fromkeys(self.names,"1.0.0");old.update(dict.fromkeys(self.names[:12],"2.0.0"))
        plan,_=self.plan(regions,old=old);self.assertEqual(len(plan.packages),24)
        self.assertFalse(set(plan.packages)&set(self.names[:24]))
    def test_physical_telemetry_has_region_evidence_and_no_unknown_vote(self):
        ledger={};candidate={"candidateId":"C","telemetrySequence":1,"delta":{"changed":dict.fromkeys(self.names[:24],"2.0.0")},"cohortSelection":{"operatorReason":"independent-intact-cohort"}}
        row=append_attempt(ledger,scope="run",config=self.config,candidate=candidate,stage="precheck",outcome="FAIL",seconds=1,
            stages=[{"stage":"tsc","outcome":"FAIL"}],size_feedback="FAIL")
        self.assertIsNotNone(row["regionId"]);self.assertEqual(row["evidenceKind"],"opaque")
        before=json.dumps(ledger["conflictRegions"],sort_keys=True);candidate["telemetrySequence"]=2
        append_attempt(ledger,scope="run",config=self.config,candidate=candidate,stage="verify-exact",outcome="UNKNOWN",seconds=1)
        self.assertEqual(before,json.dumps(ledger["conflictRegions"],sort_keys=True))
    def test_preparation_failure_is_not_opaque_shrink(self):
        ledger={};candidate={"candidateId":"C","delta":{"changed":dict.fromkeys(self.names[:24],"2.0.0")}}
        append_attempt(ledger,scope="run",config=self.config,candidate=candidate,stage="verify-exact",outcome="FAIL",seconds=1,size_feedback="FAIL",evidence_kind="unavailable")
        self.assertEqual(ledger["conflictRegions"][0]["localSoftSize"],24)
    def test_foreign_scope_does_not_transfer_regions(self):
        regions,_=self.event([],self.names[:24])
        rows,r=update_regions(regions,scope="different",config=self.config,candidate={},outcome="UNKNOWN",stage="tests")
        self.assertEqual(rows,[])
    def test_failed_child_leaves_untested_sibling_at_parent_size(self):
        regions,parent=self.event([],self.names[:24]);regions,child=self.event(regions,self.names[:12])
        self.assertEqual((regions[0]["localSoftSize"],child["localSoftSize"]),(12,6))
        self.assertEqual(child["lineage"],[parent["regionId"]])
        old=dict.fromkeys(self.names,"2.0.0");old.update(dict.fromkeys(self.names[:24],"1.0.0"))
        plan,_=self.plan(regions,old=old);self.assertEqual(plan.packages,tuple(self.names[12:24]))
    def test_same_members_new_source_have_distinct_id(self):
        regions,a=self.event([],self.names[:24]);regions,b=self.event(regions,self.names[:24],source="new")
        self.assertNotEqual(a["regionId"],b["regionId"])
    def test_timeout_finish_event_remains_unknown(self):
        from cohort_attempt_telemetry import verification_stages
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"events.jsonl"
            p.write_text(json.dumps({"event":"verify.project-check.finish","assignment":"fp","command":"tsc","outcome":"failed","exitCode":124,"durationMs":1000})+"\n")
            self.assertEqual(verification_stages(p,0,"fp")[0]["outcome"],"UNKNOWN")
    def test_production_whole_first_uses_local_fallback(self):
        regions,_=self.event([],self.names[:24])
        old=dict.fromkeys(self.names,"2.0.0");old.update(dict.fromkeys(self.names[:24],"1.0.0"))
        plan,details=plan_adaptive_cohort(incumbent=old,desired=dict.fromkeys(self.names,"2.0.0"),
            config={"cohortSchedulingStrategy":"whole-first"},ledger={"conflictRegions":regions},checkpoints=[],
            base_checkpoint_id="C0",blocked_fingerprints=[],learned_nogoods=[],fingerprint_fn=fp)
        self.assertEqual(len(plan.packages),12);self.assertTrue(details["localRegionScheduling"])
    def test_whole_first_failure_does_not_mutate_global_size(self):
        ledger={};candidate={"candidateId":"C","delta":{"changed":dict.fromkeys(self.names[:24],"2.0.0")}}
        append_attempt(ledger,scope="run",config={"cohortSchedulingStrategy":"whole-first"},candidate=candidate,
            stage="precheck",outcome="FAIL",seconds=1,size_feedback="FAIL",stages=[{"stage":"tsc","outcome":"FAIL"}])
        self.assertEqual(ledger["cohortSizeState"]["size"],24);self.assertEqual(ledger["cohortSizeState"]["failureStreak"],0)
        self.assertEqual(ledger["conflictRegions"][0]["localSoftSize"],12)


class RegionSourceIdentityTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.project=self.root/"project";self.project.mkdir()
        self.package={"name":"local-region-source","version":"1.0.0","dependencies":{"a":"1.0.0"},"scripts":{"test":"node app.js"}}
        self.write_package();(self.project/"app.js").write_text("console.log('original');")
        (self.project/"package-lock.json").write_text('{"lockfileVersion":3}')
        self.index=0
    def write_package(self):
        (self.project/"package.json").write_text(json.dumps(self.package),encoding="utf-8")
    def capture(self):
        from source_snapshot import capture_durable_source_snapshot
        self.index+=1
        snap=capture_durable_source_snapshot(self.project,self.root/f"snapshot{self.index}",timeout_seconds=0)
        return {"sourceSnapshotKey":snap.key,"sourceSnapshotContainer":str(snap.container),"projectRelative":"."}
    def test_assignment_changes_full_snapshot_but_preserves_region_hint(self):
        from cohort_conflict_regions import region_source_key
        a=self.capture();self.package["dependencies"]["a"]="2.0.0";self.package["devDependencies"]={"peer":"3.0.0"};self.write_package()
        (self.project/"package-lock.json").write_text('{"lockfileVersion":3,"changed":true}')
        b=self.capture()
        self.assertNotEqual(a["sourceSnapshotKey"],b["sourceSnapshotKey"])
        self.assertIsNotNone(region_source_key(a));self.assertEqual(region_source_key(a),region_source_key(b))
    def test_source_scripts_and_override_repairs_invalidate_hint(self):
        from cohort_conflict_regions import region_source_key
        a=region_source_key(self.capture());(self.project/"app.js").write_text("console.log('repair');")
        b=region_source_key(self.capture());self.assertNotEqual(a,b)
        self.package["scripts"]["test"]="node repaired.js";self.write_package();c=region_source_key(self.capture());self.assertNotEqual(b,c)
        self.package["overrides"]={"a":"2.0.0"};self.write_package();self.assertNotEqual(c,region_source_key(self.capture()))
    def test_unavailable_and_manifest_mismatch_do_not_invent_identity(self):
        from cohort_conflict_regions import region_source_key
        self.assertIsNone(region_source_key({}))
        checkpoint=self.capture();checkpoint["sourceSnapshotKey"]="foreign"
        self.assertIsNone(region_source_key(checkpoint))
    def test_dependency_only_acceptance_keeps_failed_region_on_next_base(self):
        from cohort_attempt_telemetry import record_attempt
        from cohort_conflict_regions import region_source_key
        from iterative_migration import save_checkpoint,save_ledger,load_ledger
        root=self.root/"run";root.mkdir();a=self.capture()
        self.package["dependencies"]["a"]="2.0.0";self.write_package();b=self.capture()
        save_checkpoint(root,{**a,"schemaVersion":1,"checkpointId":"C0","status":"VERIFIED","fullAssignment":{"a":"1.0.0"}})
        save_checkpoint(root,{**b,"schemaVersion":1,"checkpointId":"C1","status":"VERIFIED","fullAssignment":{"a":"2.0.0"}})
        save_ledger(root,{"schemaVersion":1})
        config={"cohortSchedulingStrategy":"whole-first","targets":{"a":"2.0.0"}}
        candidate={"candidateId":"candidate","baseCheckpointId":"C0","telemetrySequence":1,"delta":{"changed":{"a":"2.0.0"}}}
        run={"runId":"run","activeCheckpointId":"C0"}
        record_attempt(root,run,config,candidate,stage="precheck",outcome="FAIL",seconds=1,size_feedback="FAIL",stages=[{"stage":"tsc","outcome":"FAIL"}])
        candidate["telemetrySequence"]=2;run["activeCheckpointId"]="C1"
        record_attempt(root,run,config,candidate,stage="verify-exact",outcome="PASS",seconds=1,accepted_delta={"changed":{"a":"2.0.0"}},size_feedback="PASS")
        region=load_ledger(root)["conflictRegions"][0]
        self.assertEqual(region["sourceHintKey"],region_source_key(b));self.assertIsNotNone(region["lastProgress"])
