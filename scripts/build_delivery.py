"""Create source-only deliveries and validate the public-release prerequisites."""

import argparse
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {"app", "tests", "docs", "tools", "experiments", "schemas", "scripts", ".github"}
EXCLUDED_DIRS = {
    "__pycache__",
    ".venv",
    ".git",
    ".mypy_cache",
    ".ruff_cache",
    "test-artifacts",
    "artifacts",
    "dist",
    "data",
}


def source_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if len(relative.parts) > 1 and relative.parts[0] not in SOURCE_DIRS:
            continue
        if path.name == "DELIVERY-MANIFEST.json" or path.name.startswith(".coverage"):
            continue
        if path.name.startswith(".env") and path.name != ".env.example":
            continue
        if path.suffix in {".pyc", ".key", ".zip", ".sqlite", ".sqlite3", ".db"} or ".sqlite" in path.name:
            continue
        yield path


def verify_release(tag):
    version = (ROOT / "VERSION").read_text().strip()
    if tag != "v" + version or tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"] != version:
        raise ValueError("Tag, VERSION and pyproject version disagree")
    app_version = subprocess.check_output(
        [sys.executable, "-c", "from app.versions import APP_VERSION; print(APP_VERSION)"], cwd=ROOT, text=True
    ).strip()
    if app_version != version:
        raise ValueError("App version disagrees")
    license_text = (ROOT / "LICENSE").read_text()
    if not (license_text.startswith("MIT License") or "Version 2.0, January 2004" in license_text):
        raise ValueError("Public release blocked: owner has not selected a project license")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--verify-release")
    args = parser.parse_args()
    if args.verify_release:
        verify_release(args.verify_release)
        print("Release versions and license verified")
        return
    version = (ROOT / "VERSION").read_text().strip()
    files = list(source_files())
    manifest = {
        "version": version,
        "schema_version": "1.0.0",
        "files": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
    }
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    (ROOT / "DELIVERY-MANIFEST.json").write_bytes(encoded)
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output / f"AI-Werewolf-V{version}-Delivery.zip"
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in [(str(path.relative_to(ROOT)), path.read_bytes()) for path in files] + [
            ("DELIVERY-MANIFEST.json", encoded)
        ]:
            info = ZipInfo("ai-werewolf/" + name, date_time=(2026, 10, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(checksum + "  " + output.name + "\n")
    print(json.dumps({"file": str(output), "source_files": len(files) + 1, "sha256": checksum}))


if __name__ == "__main__":
    main()
