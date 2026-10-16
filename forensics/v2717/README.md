# V2.7.17 forensic-only diagnostics

These are CURRENT_BEHAVIOR_TEST files. They execute frozen production Kotlin
coordinator/repository/request-wrapper code and real backend request-store code
with synthetic data and injected transport/storage boundaries. No future desired
behavior is asserted and no production regression suite is modified.

From the repository root:

```
python -m unittest discover -s forensics/v2717 -p "test_*.py" -q
python forensics/v2717/run_jvm_forensics.py --java "PATH_TO_JAVA"
```

The JVM runner requires existing cached Kotlin/library JARs and Android 35's
android.jar; use --gradle-cache and --android-jar if needed. It downloads nothing
and builds no APK. JVM BuildConfig/log/DAO/request interfaces are type doubles;
Room, Compose, Android lifecycle dispatch, WorkManager engine and device network
are not emulated. Real production files remain in app/src/main and are compiled
without editing. Backend tests create only temporary local SQLite databases.

V2717E2EForensicsTest uses synthetic payloads and real Virgin River identity 88324
for timing scenarios. RequestPathForensicsTest uses explicitly synthetic ID 123;
it does not claim that ID identifies Black Mirror. Black Mirror ID and its device
WorkManager/HTTP/backend state remain NEEDS_LIVE_OBSERVATION.

No live provider/backend request, workflow dispatch, promotion, production write,
APK, source patch or version bump is part of this package. Raw captured production
datasets and credentials are excluded.
