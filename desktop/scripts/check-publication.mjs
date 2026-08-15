import { releaseBranchForAction } from "../dist-electron/publication.js";

if (releaseBranchForAction("release", "CD-42-release", "old-release", "configured-release") !== "CD-42-release") throw new Error("Release did not use the entered branch");
if (releaseBranchForAction("publish", "libs-release", "CD-42-release", "configured-release") !== "CD-42-release") throw new Error("Publication ignored the branch saved by release");
if (releaseBranchForAction("publish", "", "", "team-release") !== "team-release") throw new Error("Publication ignored the configured branch");
if (releaseBranchForAction("publish") !== "libs-release") throw new Error("Default release branch changed");
console.log("Release publication branch selection OK");