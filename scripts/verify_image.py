"""Fail closed unless a candidate matches the already-tested 1.1.0 image content.

Run inside the candidate with only this script and the public baseline mounted.
No application source override, real data volume, credentials or cloud calls.
"""
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import sys


def verify(baseline, architecture):
    if baseline.get("version") != "1.1.0":
        raise ValueError("Unexpected release baseline")
    expected_files = baseline["files"]
    root = Path("/app")
    actual_files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }
    if actual_files != expected_files:
        changed = sorted(set(actual_files) ^ set(expected_files))
        changed += sorted(name for name in set(actual_files) & set(expected_files)
                          if actual_files[name] != expected_files[name])
        raise ValueError("Application content mismatch: " + ", ".join(changed))
    packages = {item.metadata["Name"]: item.version for item in metadata.distributions()}
    if packages != baseline["packages"]:
        raise ValueError("Installed dependency versions differ from tested baseline")
    expected_python = {item["python"] for item in baseline["images"]}
    if expected_python != {platform.python_version()}:
        raise ValueError("Python version differs from tested baseline")
    if platform.machine() != {"amd64": "x86_64", "arm64": "aarch64"}[architecture]:
        raise ValueError("Unexpected runtime architecture")
    if any(Path("/data").iterdir()):
        raise ValueError("Release verification must use an empty disposable data volume")
    print(json.dumps({"verified": True, "architecture": architecture,
                      "application_files": len(actual_files), "dependencies": len(packages),
                      "python": platform.python_version()}, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args()
    try:
        verify(json.loads(Path(args.baseline).read_text()), args.architecture)
    except (ValueError, KeyError, OSError) as error:
        print("Release verification FAILED: " + str(error), file=sys.stderr)
        sys.exit(1)
