import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from verification_storage_maintenance import clean_retired_trials, retired_trials, MIN_AGE_SECONDS, storage_available
from iterative_cohort_planner import plan_adaptive_cohort

class StorageMaintenanceTests(unittest.TestCase):
    def old(self, root, name='old'):
        p=root/'trials'/f'.deploom-trial-trash-dependency-flow-baseline-verify-{name}-123-{time.time_ns()-3*MIN_AGE_SECONDS*10**9}'
        p.mkdir(parents=True);(p/'file').write_text('garbage');os.utime(p,(time.time()-3*MIN_AGE_SECONDS,)*2);return p
    def test_only_old_detached_garbage_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);garbage=self.old(root)
            keep=root/'trials'/'dependency-flow-baseline-verify-live';keep.mkdir();(keep/'source').write_text('keep')
            unknown=root/'trials'/'user';unknown.mkdir()
            result=clean_retired_trials(root)
            self.assertEqual(result['removed'],1);self.assertFalse(garbage.exists());self.assertTrue(keep.exists());self.assertTrue(unknown.exists())
    def test_recent_retirement_is_protected_even_with_old_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=self.old(root);recent=old.with_name(f'.deploom-trial-trash-dependency-flow-baseline-verify-new-123-{time.time_ns()}');old.rename(recent)
            self.assertEqual(retired_trials(root),[])
    def test_failed_delete_remains_retryable_and_never_reports_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.old(root)
            with patch('verification_storage_maintenance.shutil.rmtree',side_effect=PermissionError('busy')):
                result=clean_retired_trials(root)
            self.assertEqual(result['removed'],0);self.assertEqual(result['failed'],1)
            self.assertEqual(len(list((root/'trials').iterdir())),1)
            retry=clean_retired_trials(root,now=time.time()+2*MIN_AGE_SECONDS)
            self.assertEqual(retry['removed'],1)
    def test_linked_tree_is_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=self.old(root)
            with patch('verification_storage_maintenance._has_links',return_value=True):
                result=clean_retired_trials(root)
            self.assertEqual(result['protected'],1);self.assertTrue(p.exists())
    def test_unavailable_root_is_distinguishable_from_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertFalse(storage_available(root))   # cannot scan, not "no garbage"
            self.assertEqual(clean_retired_trials(root)['eligible'],0)
            (root/'trials').mkdir()
            self.assertTrue(storage_available(root))

class GreedyCohortTests(unittest.TestCase):
    def plan(self,names,*,config=None,blocks=(),checkpoints=(),ledger=None,old=None):
        base=old or {n:'1.0.0' for n in names};desired={n:'1.1.0' for n in names}
        fp=lambda a:hashlib.sha256(json.dumps(a,sort_keys=True).encode()).hexdigest()
        plan,details=plan_adaptive_cohort(incumbent=base,desired=desired,config=config or {},ledger=ledger or {},checkpoints=checkpoints,blocked_fingerprints=blocks,learned_nogoods=[],fingerprint_fn=fp,base_checkpoint_id=checkpoints[-1].get('checkpointId','C0') if checkpoints else 'C0')
        return plan,details,fp
    def test_thirty_packages_use_batches_and_verified_success_grows_them(self):
        names=[f'pkg-{i:02}' for i in range(30)];base={n:'1.0.0' for n in names};checkpoints=[];sizes=[]
        while any(v!='1.1.0' for v in base.values()):
            plan,details,_=self.plan(names,old=base,checkpoints=checkpoints)
            sizes.append(len(plan.packages));base=plan.assignment_dict
            checkpoints.append({'checkpointId':f'C{len(checkpoints)+1}','parentCheckpointId':f'C{len(checkpoints)}','status':'VERIFIED','acceptedDelta':{'changed':dict.fromkeys(plan.packages,'1.1.0')}})
        self.assertEqual(sum(sizes),30);self.assertEqual(sizes,[24,6])
    def test_failed_batch_is_divided_without_learning_failed_members(self):
        names=[f'pkg-{i:02}' for i in range(8)];first,_,fp=self.plan(names)
        second,details,_=self.plan(names,blocks=[fp(first.assignment_dict)])
        self.assertEqual(len(second.packages),4);self.assertNotEqual(second.assignment_dict,first.assignment_dict)
        third,_,_=self.plan(names,blocks=[fp(first.assignment_dict),fp(second.assignment_dict)])
        self.assertEqual(len(third.packages),4);self.assertTrue(set(third.packages).isdisjoint(second.packages))
    def test_families_and_requested_companions_are_selected_together(self):
        names=['@storybook/addon-a','@storybook/react','storybook','z-other']
        plan,_,_=self.plan(names,config={'cohortMaxPackages':3})
        self.assertEqual(set(plan.packages),set(names[:3]))
        plan,_,_=self.plan(['a','b','z'],config={'cohortMaxPackages':2},ledger={'scopeExpansions':[{'baseCheckpointId':'C0','packages':['a'],'companions':['z']}]})
        self.assertEqual(set(plan.packages),{'a','z'})
    def test_explicit_singleton_cap_and_priority_are_respected(self):
        plan,_,_=self.plan(['a','b'],config={'cohortMaxPackages':1,'priorityPackages':['b']})
        self.assertEqual(plan.packages,('b',))
    def test_unverified_success_never_grows_batch(self):
        plan,details,_=self.plan([f'p{i}' for i in range(20)],config={'cohortInitialPackages':8},checkpoints=[{'status':'FAILED','acceptedDelta':{'changed':dict.fromkeys(range(8))}}])
        self.assertEqual(details['batchLimit'],8)

    def test_local_peer_hints_keep_companions_near_without_becoming_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            module=Path(tmp)/'node_modules'/'a';module.mkdir(parents=True)
            (module/'package.json').write_text(json.dumps({'peerDependencies':{'z':'^1'}}))
            plan,details,_=self.plan(['a','b','z'],config={'cohortMaxPackages':2,'projectDir':tmp})
            self.assertEqual(set(plan.packages),{'a','z'});self.assertEqual(details['peerHintEdges'],1)

class OwnerProtectionTests(unittest.TestCase):
    def test_live_and_unknown_trial_owners_are_never_reaped(self):
        from verification_storage_maintenance import register_trial_owner
        from baseline_constraint_verifier import reap_orphan_verification_trials
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            live=root/'dependency-flow-baseline-verify-live';live.mkdir();register_trial_owner(live)
            unknown=root/'dependency-flow-baseline-verify-unknown';unknown.mkdir()
            for path in (live,unknown):os.utime(path,(time.time()-3*MIN_AGE_SECONDS,)*2)
            self.assertEqual(reap_orphan_verification_trials(root),(0,0))
            self.assertTrue(live.exists());self.assertTrue(unknown.exists())
    def test_real_exited_owner_can_be_reaped(self):
        import subprocess,sys
        from baseline_constraint_verifier import reap_orphan_verification_trials
        from verification_storage_maintenance import dead_trial_owner
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);trial=root/'dependency-flow-baseline-verify-dead';trial.mkdir()
            child=subprocess.Popen([sys.executable,'-c','pass']);child.wait(timeout=10)
            (trial/'.deploom-trial-owner.json').write_text(json.dumps({'pid':child.pid}))
            os.utime(trial,(time.time()-3*MIN_AGE_SECONDS,)*2)
            self.assertTrue(dead_trial_owner(trial))
            self.assertEqual(reap_orphan_verification_trials(root),(1,1))
            self.assertFalse(trial.exists())

class BudgetedSweepTests(unittest.TestCase):
    def big_old(self, root, *, name='big', files=4000):
        p=root/'trials'/f'.deploom-trial-trash-dependency-flow-baseline-verify-{name}-123-{time.time_ns()-3*MIN_AGE_SECONDS*10**9}'
        p.mkdir(parents=True)
        for i in range(files): (p/f'f{i:05}.dat').write_bytes(b'x'*200)
        os.utime(p,(time.time()-3*MIN_AGE_SECONDS,)*2);return p
    def dead_owner_trial(self, trials, *, name='big', files=0):
        import subprocess,sys
        from verification_storage_maintenance import register_trial_owner
        trial=trials/f'dependency-flow-baseline-verify-{name}';trial.mkdir()
        child=subprocess.Popen([sys.executable,'-c','pass']);child.wait(timeout=10)
        (trial/'.deploom-trial-owner.json').write_text(json.dumps({'pid':child.pid}))
        for i in range(files): (trial/f'f{i:05}.dat').write_bytes(b'x'*200)
        os.utime(trial,(time.time()-3*MIN_AGE_SECONDS,)*2)
        return trial
    def test_hot_budget_defers_oversized_tree_without_full_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=self.big_old(root)
            with patch('verification_storage_maintenance._has_links',wraps=__import__('verification_storage_maintenance',fromlist=['_has_links'])._has_links) as links:
                hot=clean_retired_trials(root,budget_bytes=4096,budget_files=64,budget_seconds=0.5)
                self.assertEqual(hot['protected'],1);self.assertEqual(hot['removed'],0)
                self.assertEqual(links.call_count,0)  # no full _has_links walk on the hot path
                self.assertTrue(p.exists())
                big=clean_retired_trials(root,budget_bytes=10*1024*1024,budget_files=100_000,budget_seconds=30)
                self.assertEqual(big['removed'],1)
            self.assertFalse(p.exists());self.assertGreater(links.call_count,0)
    def test_reap_detaches_oversized_dead_owner_tree_without_walking_it(self):
        from baseline_constraint_verifier import reap_orphan_verification_trials
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);trials=root/'trials';trials.mkdir()
            trial=self.dead_owner_trial(trials,files=4000)
            with patch('verification_storage_maintenance._has_links',wraps=__import__('verification_storage_maintenance',fromlist=['_has_links'])._has_links) as links:
                candidates,reclaimed=reap_orphan_verification_trials(trials,max_age_seconds=3600)
                self.assertEqual((candidates,reclaimed),(1,0))  # detached O(1), deletion deferred
                self.assertEqual(links.call_count,0)
            self.assertFalse(trial.exists())
            self.assertTrue(any(n.startswith('.deploom-trial-trash-dependency-flow-baseline-verify-big-') for n in os.listdir(trials)))
            finished=clean_retired_trials(root,budget_bytes=10*1024*1024,budget_files=100_000,budget_seconds=30)
            self.assertEqual(finished['removed'],1)
    def test_maintenance_clean_claims_dead_owner_normal_trial(self):
        from verification_storage_maintenance import claim_dead_owner_trials
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);trials=root/'trials';trials.mkdir()
            trial=self.dead_owner_trial(trials,name='crashed')
            self.assertEqual(claim_dead_owner_trials(root),1)
            self.assertFalse(trial.exists())
            self.assertEqual(clean_retired_trials(root)['removed'],1)
