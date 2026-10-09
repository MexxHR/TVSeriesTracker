# TV Series Tracker V2.4.0

Android aplikacija za praćenje TV serija. TMDB pruža pretragu, postere i osnovne podatke o seriji. Zaseban Official Data JSON daje verificirane službene statuse, datume i izvore. Room čuva watchlist i oba cachea. TMDB status i datumi nikada ne određuju službeni status u aplikaciji. V1 demo statusi više se ne prikazuju kao stvarni statusi.

## Tehnologije

Kotlin, Jetpack Compose, Material 3, MVVM, Room, Repository, Navigation Compose, Coroutines/Flow, DataStore Preferences, Retrofit/Gson i Coil. Min SDK 26, compile/target SDK 35, JDK 17.

## Funkcije

- 12 početnih serija u watchlisti; TMDB TV search s odgodom od 400 ms, otkazivanjem prethodnog upita i prikazom greške / Retry
- dodavanje i uklanjanje serija uz trajno spremanje u Room
- filtri, detalji serije i kronološki popis budućih premijera s potpunim datumom
- odvojena sekcija za obnovljene serije bez datuma; poster i TMDB metapodaci spremaju se kao putanje i tekst, ne bitmapovi
- hrvatski i engleski jezik te sistemska, svijetla i tamna tema
- odvojeni Official Data sloj, ručni refresh u Postavkama i automatski pokušaj pri startu kad je zadnji uspješni sync stariji od 24 sata

Bundled Android JSON sadrži 10 početnih ručno verificiranih zapisa: Tulsa King, Mayor of Kingstown, MobLand, Landman, Fallout, The Night Agent, Silo, Dutton Ranch, The White Lotus i The Gentlemen. Canonical remote JSON sinkroniziran s produkcijskim V2.3.5 feedom sada ima 12 zapisa: Lioness i The Madison dodani su verificiranim GitHub Actions objavama. Bundled asset ostaje offline fallback. Bez zapisa UI prikazuje “Nema verificiranih službenih podataka”. Spremljeni watchlist, detalji i Official Data cache rade bez interneta; TMDB pretraga zahtijeva mrežu i token. Refresh greška ne briše cache.

Silo je `FINAL_SEASON` s potvrđenim `releaseDate=2027-07-09`. Status se prikazuje kao finalna sezona, a datum ga ne mijenja u `RELEASE_DATE_CONFIRMED`. Uskoro i istoimeni filter gledaju svaki verificirani budući `releaseDate`, ne samo jedan status. Zato ondje ulaze Tulsa King (16. 10. 2026.) i Silo (9. 7. 2027.).

## Official Data JSON

Canonical datoteka je `official-data/official_series_data.json`, a kopija za prvi start i offline instalaciju je `app/src/main/assets/official_series_data.json`. Obje održavajte jednakima pri novom izdanju. Contract je u `official-data/schema.json`. Primjer jednog **strukturnog** zapisa (nije tvrdnja o stvarnoj seriji):

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-09-26T12:00:00Z",
  "series": [{
    "tmdbId": 123456,
    "title": "Example",
    "nextSeasonNumber": 3,
    "status": "RENEWED",
    "releaseDate": null,
    "releaseYear": null,
    "sourceName": "Official network press",
    "sourceUrl": "https://example.com/official-announcement",
    "announcementDate": "2026-09-20",
    "lastChecked": "2026-09-26"
  }]
}
```

`schemaVersion` ostaje 1; `generatedAt` je UTC ISO-8601 vrijeme generiranja cijelog feeda i mora rasti pri promjeni. `tmdbId` je jedinstveni pozitivni TMDB TV ID; `title` je urednička pomoć i ne zamjenjuje TMDB naslov u UI-u. `nextSeasonNumber` je pozitivni broj ili `null`. `status` dopušta samo `RENEWED`, `RELEASE_DATE_CONFIRMED`, `FINAL_SEASON`, `CANCELED`; odsutan zapis znači *nije verificirano u našoj bazi*. `releaseDate` je nezavisno verificiran ISO datum ili `null`, obavezan za `RELEASE_DATE_CONFIRMED`, ali dopušten i uz `FINAL_SEASON`. `releaseYear` je godina ili `null`; ako su oba zadana, godina mora odgovarati datumu. `sourceName` je naziv originalnog službenog izvora, `sourceUrl` njegov HTTPS URL, `announcementDate` datum službene objave ili `null`, a `lastChecked` ISO datum uspješne provjere koja je proizvela objavljenu činjenicu. Aplikacija svaki valjan zapis tretira kao `sourceType=OFFICIAL`, zato u feed smiju ući samo ručno provjerene originalne objave mreže ili studija. TMDB, IMDb, Wikipedia, Reddit i zabavni portali nisu službeni izvori.

Za The White Lotus canonical izvor je [WBD/HBO objava o početku produkcije četvrte sezone](https://press.wbd.com/us/media-release/hbo-0/hbo-original-white-lotus-season-4-begins-filming-france) od 15. 4. 2026.; `announcementDate` je datum upravo te objave, a ne ranije obnove. Za Fallout Amazonov članak navodi datum objave 12. 5. 2025. u metapodacima, dok tekst ažuriranog članka navodi 13. 5. 2025. kao izvorni datum objave; dataset zadržava datum službene najave naveden u zahtjevu. Članak je ažuriran 19. 6. 2026. i još ne navodi datum premijere treće sezone.

Za dodavanje serije provjerite originalni press URL, pronađite točan TMDB TV ID, popunite sva polja, povećajte `generatedAt`, validirajte JSON, uredite canonical datoteku i kopirajte je u asset za sljedeći build. Nepoznati status, duplikat ID-a, nevaljan HTTPS URL, nedostajući obavezni podatak ili proturječan datum odbacuju cijeli novi payload; zadnji valjani cache ostaje. Valjan noviji feed može namjerno imati prazan `series` niz, što uklanja ranije verificirane zapise; zato objavu feeda treba urednički provjeriti.

## Remote sync

Zadani produkcijski URL za normalni i debug build je `https://raw.githubusercontent.com/MexxHR/TVSeriesTracker/main/official-data/official_series_data.json`. Ugrađuje se u BuildConfig. Po potrebi ga možete nadjačati nepraznim `OFFICIAL_DATA_URL` u Gradle propertyju, environment varijabli ili ignoriranom `local.properties`; prazan GitHub Actions variable ne isključuje zadani URL. Bundled JSON ostaje fallback za prvi start i offline rad. Pri nadogradnji aplikacije noviji bundled dataset zamjenjuje stariji cache, ali ne prepisuje još noviji remote cache. UI ne čita JSON i ne poziva mrežni izvor: Repository dohvaća JSON, validira cijeli payload, uspoređuje `generatedAt`, atomarno zamjenjuje Official Data tablicu samo novijim payloadom, a Room Flow ažurira ekran. Ručni refresh je u Postavkama. Ako valjani remote ima isti ili stariji `generatedAt`, cache se ne zamjenjuje, posljednja uspješna provjera se bilježi i UI prikazuje “Već je ažurno”. Za offline provjeru isključite mrežu i ponovite refresh; prethodni podaci ostaju.

## V2.3.5 Official Data automatizacija

Workflow `.github/workflows/update-official-data.yml` provjerava 12 registriranih serija svaki dan u 07:17 UTC. U **Actions → Update official data → Run workflow** može se pokrenuti i ručno; `dry_run=true` je zadana vrijednost. **Prvi GitHub Actions run nakon V2.3.5 neka bude dry-run.** Schedule ostaje u dry-run modu dok nakon pregleda tog rezultata ne postavite Actions variable `OFFICIAL_DATA_LIVE_ENABLED=true`; zatim radi automatski. Registry je `official-data/sources.json`; moduli za Paramount, Netflix, Apple, WBD i Amazon nalaze se u `official-data/automation/update.py`. V2.3.1 dodao je službenu Paramount+ Season 3 objavu za The Madison i izravne WBD press fallbackove. V2.3.2 dodaje ograničen retry za prolazne HTTP greške, uključujući IncompleteRead. V2.3.3 spaja zasebne renewal i premiere činjenice za istu sezonu, prepoznaje oblik Season Three i sprječava čitanje znamenki godine kao broja sezone; WBD ostaje prijavljen kao nedostupan dok se ne potvrdi pouzdan službeni kanal. V2.3.4 razdvaja obnovu MobLand S3 od datuma premijere S2 u istoj rečenici; slab ili nepovezan datum ne ruši verificiranu obnovu. Pri jasnom službenom dokazu workflow validira promjenu, upisuje canonical JSON i JSONL audit te commita samo stvarne promjene. Nedostupan provider prijavljuje upozorenje i ostavlja postojeći zapis (uključujući lastChecked) netaknutim; verified promjene drugih serija mogu se objaviti nakon potpune validacije. Konfliktni dokazi, globalne sigurnosne greške i programske iznimke blokiraju cijeli publish. Dry-run prikazuje publishable bez pisanja; audit sadrži samo stvarno objavljene promjene. Prag zaštite je najviše četiri promijenjene serije u jednom runu. Nisu potrebni AI API, plaćena usluga ni osobni PAT. Detaljna pravila detekcije, prioriteti, ograničenja, dodavanje izvora i lokalni test/dry-run opisani su u [automation README](official-data/automation/README.md).

## V2.4 Phase 1 discovery

Read-only CLI `python official-data/automation/update.py --discover <TMDB_TV_ID>` pronalazi potencijalne izvore na službenim domenama. TMDB služi samo za identitet i usmjeravanje prema provideru; nikad za verificirani status ili datum. Rezultat ne mijenja `sources.json`, canonical JSON, audit ni Android watchlist. Odvojeni ručni GitHub workflow **Discover official source (read-only)** prihvaća TMDB TV ID; dnevni production workflow ostaje nepromijenjen. Detalji routing pravila, pet live proba, ograničenja i siguran model zahtjeva opisani su u [discovery dokumentaciji](official-data/automation/DISCOVERY.md).

## Phase 2B.1: zahtjev za monitored obradu

Nakon uspješnog lokalnog dodavanja TMDB serije WorkManager sprema jedinstveni
zahtjev i, kad je mreža dostupna, šalje samo TMDB ID na javni backend
`POST /v1/series-requests`. Neuspjeh tog transporta ne uklanja seriju iz
watchlista. `queued` i `already_queued` nisu verified statusi. Backend pokreće
strogo ograničeni workflow `process-series-request.yml`, koji piše samo u
monitored staging datoteke. Production Official Data sync i UI ostaju odvojeni.

Backend je dostupan na `https://mexxhr.pythonanywhere.com` i ta javna adresa
zadana je vrijednost `SERIES_REQUEST_API_BASE_URL` za Android build. Može se
nadjačati Gradle propertyjem, environment varijablom ili lokalnom postavkom.
`GITHUB_TOKEN`, `TMDB_API_TOKEN` i ostali backend credentials ostaju server-side.
Android više ne ugrađuje TMDB token. Backend služi fiksne read-only TMDB
metadata rute za search/details; TMDB i dalje nije official lifecycle izvor.
Deployment, trust granice, limiti, privatnost i retry pravila opisani su u
[Phase 2B arhitekturi](official-data/automation/PHASE_2B_ARCHITECTURE.md).

Lokalni backend fake dispatch test:

```bash
python -m unittest discover -s backend -p 'test_*.py' -q
python backend/dev_server.py
```

`dev_server.py` sluša samo na loopback adresi i ne pokreće GitHub Actions.

Ova aplikacija prikazuje TMDB atribuciju u Postavkama: **“This product uses the TMDB API but is not endorsed or certified by TMDB.”** Koristi [TMDB službeni odobreni logo](https://www.themoviedb.org/about/logos-attribution), neizmijenjen u `app/src/main/res/raw/tmdb_logo.svg`. [TMDB pravila atribucije](https://developer.themoviedb.org/docs/faq) zahtijevaju logo i navedenu obavijest u odjeljku About/Credits.

## Gradnja

Otvorite projekt u Android Studiju ili koristite priloženi Gradle wrapper i JDK 17:

```bash
./gradlew :app:assembleDebug
```

APK nastaje u `app/build/outputs/apk/debug/app-debug.apk`. `./gradlew :app:testDebugUnitTest` pokreće testove parsera i ponašanja cachea.

## GitHub Actions APK

Pushajte cijeli projekt u GitHub repozitorij. `OFFICIAL_DATA_URL` variable potreban je samo ako želite nadjačati zadani URL. `SERIES_REQUEST_API_BASE_URL` je javna build konfiguracija za backend; backend secrets ne idu u Android build. U kartici **Actions** odaberite **Android debug APK** i **Run workflow**. Nakon uspješnog builda preuzmite artifact `TVSeriesTracker-V2.6.0-debug`, raspakirajte ga i instalirajte APK na telefon. Workflow radi i na push u `main` te na pull request.

**Nadogradnja bez brisanja podataka:** Android zahtijeva isti potpis za prethodni i V2.6.0 APK. GitHub Actions na novom runneru inače generira novi debug ključ; za Actions APK koji mora ažurirati postojeću instalaciju dodajte repository secret `ANDROID_DEBUG_KEYSTORE_BASE64` sa Base64 sadržajem **istog** `debug.keystore` kojim je potpisan instalirani APK. Workflow ga koristi samo ako je secret postavljen.

**Phase 2B.3 upload:** V2.6.0 ZIP namjerno ne sadrži pet live Official Data datoteka: canonical JSON, produkcijski audit, `sources.json`, monitored registry i monitored audit. GitHub main već ima prihvaćeni ONE PIECE production zapis i žive monitored zapise. Prenesite kod kao dopunu postojećem mainu; sačuvajte svih pet postojećih datoteka. ZIP nije samostalan novi checkout. `OFFICIAL_DATA_PROMOTION_ENABLED` ostavite isključenim dok ne pregledate implementaciju i testove; zatim ga operater može zasebno postaviti na točno `true`.

## Struktura

- `data/Series.kt` — domenski model i legacy katalog naslova/postera; demo statusi uklanjaju se prije UI-a
- `data/OfficialData.kt` — validacija JSON-a i verified domenski model
- `data/OfficialDataRepository.kt` — HTTPS izvor, bundled fallback i Room cache
- `data/TrackingDatabase.kt` — Room watchlist, TMDB/Official cache i eksplicitne migracije 1 → 2 → 3
- `data/SeriesRepository.kt` — spajanje lokalnog watchlista, TMDB i Official Data izvora; UI ne zna za JSON ili Room
- `data/remote/` — TMDB API, DTO modeli i mapiranje u domenski/Room model
- `data/SettingsRepository.kt` — DataStore postavke
- `SeriesViewModel.kt` — radnje i životni ciklus UI-a
- `MainActivity.kt` — Compose ekrani, navigacija i tema

Migracija 1 → 2 zadržava `tracked_series.id`, dodaje nullable `tmdbId` i novu tablicu `series_metadata`. Migracija 2 → 3 samo stvara `official_series_data` i `official_sync_state`; ne mijenja watchlist, TMDB cache ni DataStore. Nema `fallbackToDestructiveMigration` i migracija ne pokreće početno seedanje. Metapodaci iz TMDB-a nikada ne prepisuju službeni status, datum objave ni izvor.

V2.4.0 ne mijenja Room shemu (ostaje verzija 3) ni postojeći verified dataset. Automatizacija je opisana u `official-data/automation/README.md`.

## Planirano

V2.4: širenje registryja i dodatni službeni discovery kanali za nove serije.

