"""Fail release checks if packaged outputs contain credential material."""
import re
import sys
from pathlib import Path
from zipfile import ZipFile


PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
]


def secret_values(project: Path):
    values = []
    props = project / "local.properties"
    if props.exists():
        for line in props.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, separator, value = line.partition("=")
            if separator and any(word in key.upper() for word in ("TOKEN", "SECRET", "KEY")) and len(value) >= 12:
                values.append(value.strip().encode())
    return values


def check(project: Path, apk: Path, archive: Path):
    known = secret_values(project)
    with ZipFile(archive) as zipped:
        names = zipped.namelist()
        gradle_path = "app/build.gradle.kts"
        if gradle_path not in names or zipped.read(gradle_path) != (project / gradle_path).read_bytes():
            raise ValueError("Android Gradle configuration is missing or stale in project ZIP")
        forbidden_names = ("local.properties", ".keystore", ".jks", ".p12", ".gradle/", "/build/")
        if any(name.endswith(forbidden_names[:4]) or any(part in name for part in forbidden_names[4:])
               for name in names):
            raise ValueError("Forbidden file in project ZIP")
        for name in names:
            data = zipped.read(name)
            if any(pattern.search(data) for pattern in PATTERNS) or any(value in data for value in known):
                raise ValueError(f"Credential-like value in project ZIP entry: {name}")
    with ZipFile(apk) as packaged:
        for name in packaged.namelist():
            data = packaged.read(name)
            if any(pattern.search(data) for pattern in PATTERNS) or any(value in data for value in known):
                raise ValueError(f"Credential-like value in APK entry: {name}")
    for relative in ("app/build.gradle.kts", "app/src/main/java/com/example/tvseriestracker/data/remote/TmdbRemoteDataSource.kt"):
        source = (project / relative).read_text(encoding="utf-8")
        if "BuildConfig.TMDB_API_TOKEN" in source or 'buildConfigField("String", "TMDB_API_TOKEN"' in source:
            raise ValueError("Android build still embeds TMDB token")
    return len(names)


if __name__ == "__main__":
    count = check(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    print(f"Artifact credential checks passed; {count} project ZIP entries")
