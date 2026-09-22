# Project Rules & Environment Roles

## ⚠️ CRITICAL RULE: Environment Roles & File Safety

- **`C:\` (Local Workspace)**: **TEST / DEVELOPMENT ENVIRONMENT**. All code changes, testing, experimental runs, and development must happen strictly here.
- **`Z:\` (Server / Remote Drive)**: **PRODUCTION ENVIRONMENT**.
- **NEVER DIRECTLY OVERWRITE OR AUTOMATICALLY BULK-SYNC TO `Z:\`**. Production data on `Z:\` must be protected from accidental overwrites. Any updates to production must be explicitly reviewed and handled with extreme care.

## 📖 DOCUMENTATION & GIT WORKFLOW

- **Pidä README.md ja dokumentaatio aina ajan tasalla**: Kun järjestelmään lisätään uusia ominaisuuksia, työkaluja tai arkkitehtuurimuutoksia, päivitä `README.md` aina vastaamaan koodin todellista tilaa.
- **Vie muutokset gittiin (Commit & Push)**: Kun muutos on valmis ja testattu, vie se heti gittiin (`git add`, `git commit` ja `git push origin main`).
- **Kommentoi ja raportoi**: Kirjoita selkeä commit-viesti ja kerro käyttäjälle tiiviisti ja tarkasti, mitä on tehty ja testattu.


