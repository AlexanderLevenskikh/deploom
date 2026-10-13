"""Safe identity-component diffs: hashes and labels, never raw spec/config values."""
import re
from urllib.parse import urlsplit

def identity_component_diff(expected, observed, *, limit=20):
    before=dict(expected.get("components") or {});after=dict(observed.get("components") or {})
    changes=[{"component":key,"expected":before.get(key),"observed":after.get(key)}
             for key in sorted(set(before)|set(after)) if before.get(key)!=after.get(key)]
    return {"changedComponentCount":len(changes),"changedComponents":changes[:limit]}

def safe_identity_error(error):
    def redact(match):
        try:
            parsed=urlsplit(match.group());host=parsed.hostname or "redacted"
            return parsed.scheme+"://"+host+"/<redacted>"
        except ValueError:return "<redacted-url>"
    return re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"']+",redact,str(error))[:2000]


def manifest_entry_change_summary(before_entries, after_entries, *, limit=20):
    before = {str(item["path"]): item for item in before_entries}
    after = {str(item["path"]): item for item in after_entries}
    changes = [{"path": name, "change": "missing" if name not in after else "added" if name not in before else "modified"}
               for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]
    return {"changedPathCount": len(changes), "changedPaths": changes[:limit]}


def source_manifest_change_summary(snapshot, observed, *, limit=20):
    import json
    try:
        record=json.loads((snapshot.container/"manifest.json").read_text(encoding="utf-8-sig"))
        before={str(item["path"]):item for item in record["entries"]}
        after={str(item["path"]):item for item in observed.entries}
        changes=[{"path":name,"change":"missing" if name not in after else "added" if name not in before else "modified"}
                 for name in sorted(set(before)|set(after)) if before.get(name)!=after.get(name)]
        return {"changedPathCount":len(changes),"changedPaths":changes[:limit]}
    except (OSError,ValueError,KeyError,TypeError):
        return {"changedPathsUnavailable":True}
