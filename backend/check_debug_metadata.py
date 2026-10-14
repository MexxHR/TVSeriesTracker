"""Reject a stale or unexpected Gradle debug APK output before artifact upload."""
import json
from pathlib import Path
import re
import sys


def check(metadata_path, gradle_path):
    metadata = json.loads(Path(metadata_path).read_text(encoding='utf-8'))
    source = Path(gradle_path).read_text(encoding='utf-8')
    application_id = re.search(r'applicationId\s*=\s*"([^"]+)"', source)[1]
    code = int(re.search(r'versionCode\s*=\s*(\d+)', source)[1])
    name = re.search(r'versionName\s*=\s*"([^"]+)"', source)[1]
    elements = metadata.get('elements', [])
    if (metadata.get('applicationId') != application_id or metadata.get('variantName') != 'debug'
            or len(elements) != 1 or elements[0].get('versionCode') != code
            or elements[0].get('versionName') != name
            or elements[0].get('outputFile') != 'app-debug.apk'):
        raise ValueError('Debug APK output metadata does not match current source')
    apk = Path(metadata_path).parent / 'app-debug.apk'
    if not apk.is_file() or apk.stat().st_size == 0:
        raise ValueError('Debug APK is missing or empty')
    return {'applicationId': application_id, 'versionCode': code, 'versionName': name,
            'variant': 'debug', 'outputFile': str(apk)}


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    print(json.dumps(check(sys.argv[1], root / 'app/build.gradle.kts')))
