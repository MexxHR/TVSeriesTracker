"""Package source-only ZIP and the built debug APK."""
from pathlib import Path
from shutil import copyfile
from zipfile import ZipFile, ZIP_DEFLATED

from check_release_artifacts import check


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT.parent
VERSION = "V2.7.0"
APK = OUT / f"TV-Series-Tracker-{VERSION}-debug.apk"
ZIP = OUT / f"TVSeriesTracker-{VERSION}.zip"
TOP = [".gitignore", "build.gradle.kts", "app/build.gradle.kts", "gradle.properties", "gradlew", "gradlew.bat",
       "README.md", "settings.gradle.kts"]
DIRS = [".github/workflows", "app/src", "backend", "gradle/wrapper", "official-data"]
SKIP_NAMES = {"__pycache__", ".gradle", ".gradle-user", "build", "local.properties", ".pytest_cache"}
PRESERVE_LIVE_STATE = {
    "official-data/official_series_data.json",
    "official-data/history/changes.jsonl",
    "official-data/sources.json",
    "official-data/monitored_series.json",
    "official-data/history/monitored_changes.jsonl",
}


def package():
    source_apk = ROOT / "app/build/outputs/apk/debug/app-debug.apk"
    if not source_apk.is_file():
        raise FileNotFoundError(source_apk)
    copyfile(source_apk, APK)
    files = [ROOT / name for name in TOP]
    for directory in DIRS:
        files.extend(path for path in (ROOT / directory).rglob("*") if path.is_file()
                     and not set(path.relative_to(ROOT).parts) & SKIP_NAMES
                     and path.relative_to(ROOT).as_posix() not in PRESERVE_LIVE_STATE
                     and path.suffix not in {".pyc", ".apk", ".jks", ".p12", ".keystore"})
    with ZipFile(ZIP, "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(ROOT).as_posix())
    count = check(ROOT, APK, ZIP)
    print(f"Packaged {VERSION}: {count} source entries; credential checks passed")


if __name__ == "__main__":
    package()
