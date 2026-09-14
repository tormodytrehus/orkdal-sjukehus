# Orkdal sjukehus-monitor

Én automatisk RSS-feed for redaksjonell overvåking av Orkdal sjukehus. Treffene
kommer fra St. Olavs hospital, Helse Midt-Norge, styrepapirer, postjournal,
tilsyn, stillingsannonser, Doffin, ventetider, kvalitetsdata, NPE og kommunale
saker om legevakten.

## Slik tas den i bruk

1. Opprett et offentlig GitHub-repository og legg inn disse filene.
2. Åpne **Settings → Pages**.
3. Velg **Deploy from a branch**, `main` og mappen `/docs`.
4. Åpne **Actions → Oppdater Orkdal-feed → Run workflow**.
5. Feed-adressen blir:
   `https://BRUKERNAVN.github.io/REPOSITORY/orkdal-sjukehus.xml`

Legg denne adressen i RSS.app eller redaksjonens RSS-leser. Statussiden ligger på
`https://BRUKERNAVN.github.io/REPOSITORY/`.

## Hvordan den virker

- GitHub Actions kjører hver time.
- Vanlige nettsider leses direkte.
- PDF- og DOCX-vedlegg tekstsøkes.
- JavaScript-sider og tjenester med tilgangskontroll overvåkes gjennom presise
  Google News-kildesøk som reserve.
- Treff dedupliseres etter normalisert URL.
- Historikk lagres i `data/items.json`.
- Feil i én kilde stopper ikke de andre. Resultatet vises i `docs/status.json` og
  på statussiden.

Første kjøring begrenses til åtte treff per kilde, slik at feeden ikke fylles med
hele det historiske arkivet.

## Redigere kilder og søkeord

Alt redaksjonen normalt trenger å endre ligger i `config.yml`. Legg til eller
fjern søkeord under `keywords`. Kildetyper:

- `html_links`: leser lenker fra en side og kan tekstsøke dokumentene.
- `google_news`: presist nettstedssøk via Google News RSS.
- `page_snapshot`: varsler når en hel side endres.

Etter en endring kan workflowen startes manuelt fra Actions.

## Lokal test

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
python monitor.py --dry-run
python monitor.py
```

## Viktige begrensninger

- Et treff er et tips, ikke en publiseringsklar opplysning.
- SharePoint, Doffin, Helsenorge og Power BI kan endre teknisk løsning eller
  blokkere automatiske forespørsler. Kildestatus gjør slike feil synlige.
- Google News-reservene kan være forsinket og kan gå glipp av dokumenter.
- Google-treff filtreres en ekstra gang på eksplisitte Orkdal-relaterte ord for
  å holde generelle sykehussaker ute av strømmen.
- Ventetidssidene er ikke én stabil, åpen Orkdal-feed. Første versjon overvåker
  nye indekserte Orkdal-resultater og endringer i kvalitetskildene. Konkrete
  terskelvarsler per behandling krever at aktuelle behandlinger/BID-er velges.
- Ikke legg API-nøkler i repoet. Bruk GitHub Actions Secrets dersom en kilde
  senere får en egen API-avtale.

## Lisens og kildebruk

Koden er MIT-lisensiert. Innholdet tilhører de respektive kildene. RSS-elementene
lenker til originalkilden og må brukes i tråd med kildevilkårene.
