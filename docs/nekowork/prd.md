# PRD Nekowork — Pengganti Nekorp di atas SoduWork

- Versi: 0.1
- Last updated: 2026-11-20
- Owner: Kamisama / Nekorp
- Basis: fork `yughoz/SoduWork` (MIT, mdiri-ai/SoduWork) — rebrand Nekowork
- Kaneo: project NW (workspace Nekowork)

## 1. Tujuan

Nekowork = lapisan "Corporate AI Operations" Nekorp yang diangkut ke atas engine SoduWork (issue → agent → review → sign-off). V1 fokus 4 target:

1. **Integrasi user ke llm.nekotekno** — tiap user punya 1 token New API self-service (generate/reset/revoke, show-once).
2. **Integrasi ke Asana** — create/complete task, resolve assignee via email mapping, cache users/projects.
3. **MCP Nekowork** — port Super MCP Nekorp (`/api/mcp`) jadi MCP endpoint di Hub, konsumen lama (Hermes, Maus, Cursor) tetap jalan.
4. **Dashboard sisa token** — user lihat saldo/usage token; tampil di admin console dan di desktop client (sebagai app).
5. **Titen memory embedded** — server Titen milik Nekowork di dalam Hub (bukan instance eksternal), key per-user dikelola admin, client auto-setup pas login.

## 2. Keputusan terkunci (2026-11-20)

| Keputusan | Nilai |
|---|---|
| Org model v1 | Single-org internal Nekoding; multi-tenant (corporates + gating) menyusul kalau perlu |
| Gateway LLM | Cuma New API / Nekotekno (`llm.nekotekno.xyz`). LiteLLM/Vision gak ikut v1 |
| Tempat PRD | `docs/nekowork/` di repo fork |

## 3. Stack

| Layer | Teknologi |
|---|---|
| Client | Electron (SoduWork client, existing) |
| Hub backend | FastAPI + SQLAlchemy + Alembic (existing, kita extend) |
| Admin console | React (hub-frontend, existing) |
| DB | Hub DB (SQLite lokal / Postgres Docker) — tabel baru via Alembic, tanpa PocketBase |
| LLM gateway | New API `llm.nekotekno.xyz` (HTTP management API) |
| Asana | Via Maton gateway (`MATON_API_KEY` + connection id), sama seperti Nekorp |
| MCP | Streamable HTTP di Hub, `mcp` python SDK |
| Memory | Titen (`titen-memory`, Bun + SQLite) — child process Hub, loopback :18787, proxy `/api/v1/titen/mcp` |
| Port | Tetap 18989 (loopback) — semua fitur numpang Hub, gak ada port baru |

## 4. Arsitektur

```text
[Electron client]          [Admin console]            [MCP clients: Hermes/Maus/Cursor]
      |  app "Token"             |  page /tokens                |  Bearer mcp_tokens
      +────────────+─────────────+──────────────────────────────+
                   v
            [SoduWork Hub :18989]
              ├─ existing: issues, plugins, review, audit, model catalog
              └─ NEKOWORK EXTENSIONS
                  ├─ modul_gateway   → New API (provisioning, token CRUD, saldo)
                  ├─ modul_asana     → Maton (task write, cache users/projects)
                  ├─ modul_mcp       → /mcp streamable HTTP (6 tools)
                  ├─ modul_titen     → titen serve embedded + proxy /titen/mcp + key per-user
                  └─ modul_token_dashboard → UI saldo (console + client app)
```

User Nekowork = user Hub (auth existing SoduWork). Roles: `member` dan `admin` (mapping dari role Hub yang ada; kalau Hub cuma kenal admin/non-admin, member = semua non-admin).

## 5. Modul

| Modul | Isi |
|---|---|
| [modul_gateway.md](modul_gateway.md) | Provisioning user + token lifecycle di New API |
| [modul_asana.md](modul_asana.md) | Integrasi Asana via Maton + cache + identity mapping |
| [modul_mcp.md](modul_mcp.md) | MCP Nekowork: port 8 tools Super MCP (6 lolos v1) |
| [modul_titen.md](modul_titen.md) | Titen memory embedded: server, key per-user, management, client auto-setup |
| [modul_token_dashboard.md](modul_token_dashboard.md) | Dashboard saldo/usage, console + client app |

## 5b. IM Telegram (native upstream, tanpa build baru)

Hasil diskusi 2026-11-20: adapter Telegram **sudah ada bawaan SoduWork** (`services/im_provider_registry.py`, `im_adapters.py` — webhook secret `x-telegram-bot-api-secret-token`, chat pribadi langsung, grup pakai `/pair@bot` & `/soduwork@bot`, dedup `update_id`). Yang perlu dilakukan cuma **aktivasi**:

1. Bikin bot via BotFather → `bot_token` + `webhook_secret` disimpan NekoVault (`api/nekowork-telegram-bot`), gak pernah plaintext di chat.
2. Set webhook ke URL publik Hub — hub lokal loopback, jadi butuh tunnel (infra cloudflared lelevo sudah ada) atau diisi pas Phase 5 deploy.
3. Admin isi koneksi IM di console (field `bot_token`, `bot_username`, `bot_id`, `webhook_secret`), member pair pakai kode pairing.
4. Hint setup bawaan masih zh → masuk cleanup i18n (Phase 5).
5.catatan: fitur IM upstream gak di-hide sama sekali (keputusan diskusi) — adapter DingTalk/WeCom/Feishu/Teams/Slack dibiarkan utuh.

## 6. Milestone

- **Phase 1 — Gateway (≈ pekan 1):** tabel `newapi_accounts`, provisioning, generate/reset/revoke token, saldo live. Batis dari semua modul lain.
- **Phase 2 — Dashboard (≈ pekan 1):** page saldo/usage di console + app di client. Bareng gateway biar langsung keliatan hasilnya.
- **Phase 3 — Asana (≈ pekan 2):** cache users/projects, `user_integrations`, create/complete task via Maton.
- **Phase 4 — MCP (≈ pekan 2):** endpoint `/mcp`, `mcp_tokens`, 6 tools, audit, smoke test dari Hermes/Maus.
- **Phase 4b — Titen memory (≈ pekan 2-3):** `titen serve` embedded, key per-user + fence project, management admin, auto-setup client via personal MCP, proxy `/api/v1/titen/mcp` (lihat modul_titen.md).
- **Phase 5 — Rebrand & deploy (≈ pekan 3):** cleanup i18n (bug NW-1 #1), harness Hermes, aktivasi IM Telegram (lihat §5b), deploy ke server (keputusan deploy masih open).

## 7. Out of scope v1

- Multi-tenant corporates + module gating per corporate
- LiteLLM / Vision sebagai gateway kedua (dual keys)
- Pipeline Meetings (Vexa bot → MoM → draft task) — tetap hidup di Nekorp sampai diport
- GitLab activity sync — konsekuensinya 2 tool MCP (`repo_activity`, `daily_digest`) gak ikut v1
- Ops gateway lanjutan: daily balance reset otomatis, nekopro window policy engine
- OAuth per-agent token MCP, allowlist tool per-token, Sentry, cat-park
- Hide/cabut fitur upstream (legal, update channel, IM adapters lain, remote_screen, mail) — dibiarkan utuh (keputusan 2026-11-20); GitLab native sync, meeting plugin, dan MCP self-service token gak masuk roadmap saat ini

## 8. Open questions

1. Deploy: `ops/self-host` Docker vs PM2 lelevo + tunnel (keputusan NW-1 yang belum).
2. Branding final: nama/logo/domain (corp.nekoding.xyz pindah atau domain baru?).
3. Data lama Nekorp (PocketBase :8091): migrasi `user_integrations` + cache Asana ke Hub DB, atau fresh start? (Rekomendasi: migrasi `user_integrations` aja, cache di-refresh.)
4. Model group default buat token baru (Nekorp pakai `AssignedModelPolicy` per corporate; v1 usul satu policy global: `nekolite` default).
5. Titen: project shared antar user (`org`) diaktifin dari awal atau per-user aja dulu? (Rekomendasi: per-user aja; shared belakangan kalau diminta.)

## 9. Glossary

- **New API** — gateway one-api style di `llm.nekotekno.xyz`; token user + quota + log ada di sana.
- **Maton** — gateway API eksternal (Asana dll) yang dipakai Nekorp; auth via `MATON_API_KEY`.
- **Show-once** — plaintext token cuma tampil sekali saat generate/reset; setelah itu masked.
- **Harness** — runtime agent (Pi, Claude Code, dll) yang jalan di bawah Hub SoduWork.
