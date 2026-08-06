# Project Rules & Environment Roles

## ⚠️ CRITICAL RULE: Environment Roles & File Safety

- **`C:\` (Local Workspace)**: **TEST / DEVELOPMENT ENVIRONMENT**. All code changes, testing, experimental runs, and development must happen strictly here.
- **`Z:\` (Server / Remote Drive)**: **PRODUCTION ENVIRONMENT**.
- **NEVER DIRECTLY OVERWRITE OR AUTOMATICALLY BULK-SYNC TO `Z:\`**. Production data on `Z:\` must be protected from accidental overwrites. Any updates to production must be explicitly reviewed and handled with extreme care.
