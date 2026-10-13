"""Same physical workspace must seal and cold-replay to the same artifact key."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from artifact_integrity import ArtifactIntegrityError, build_artifact_tree_integrity
from reparse_materialization import ReparseLink, inventory_reparse_plan, ReparseMaterializationError

class ArtifactReparseCanonicalizationTests(unittest.TestCase):
    def test_declared_workspace_and_cold_inventory_have_identical_physical_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve();lib=root/"packages/lib";lib.mkdir(parents=True)
            (lib/"package.json").write_text('{"name":"fixture-lib"}')
            (lib/"index.cjs").write_text("module.exports=42;")
            modules=root/"node_modules";modules.mkdir();link=modules/"fixture-lib"
            if os.name=="nt":
                result=subprocess.run([os.environ.get("COMSPEC","cmd.exe"),"/d","/c","mklink","/J",str(link),str(lib)],capture_output=True,text=True)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
            else:link.symlink_to(lib,target_is_directory=True)
            declared=inventory_reparse_plan(root,workspace_package_targets={"fixture-lib":lib})
            self.assertEqual("workspace-private-upper",declared[0].authority)
            sealed=build_artifact_tree_integrity(root,reparse_plan=declared)
            cold=build_artifact_tree_integrity(root)
            self.assertEqual(sealed.key,cold.key)
            self.assertEqual(sealed.source_content_key,cold.source_content_key)
            wrong=root/"other";wrong.mkdir();(wrong/"package.json").write_text('{"name":"wrong"}')
            with self.assertRaisesRegex(ReparseMaterializationError,"WORKSPACE_TARGET_MISMATCH"):
                inventory_reparse_plan(root,workspace_package_targets={"fixture-lib":wrong})
            invalid=(ReparseLink("node_modules/fixture-lib","other",declared[0].link_kind,"workspace-private-upper","fixture-lib"),)
            with self.assertRaisesRegex(ArtifactIntegrityError,"target changed"):
                build_artifact_tree_integrity(root,reparse_plan=invalid)

if __name__=="__main__":unittest.main()
