# Modul: Integrasi Asana (via Maton)

> Nekowork bisa baca cache users/projects Asana dan create/complete task dengan assignee hasil mapping email.

---

## 1. Tujuan

Port integrasi Asana Nekorp (`asana-write.ts`, `asana_users_cache`, projects `ai_enabled`) ke Hub Nekowork. V1 gak termasuk pipeline meeting (Vexa) — cuma lapisan data + tulis task.

Semua tulis ke Asana lewat **Maton** (sama kayak Nekorp MCP M4 yang udah terverifikasi e2e), bukan client Asana langsung.

## 2. Field Spesifikasi

### `asana_users_cache`

| Field | Type | Required | Validasi/Notes |
|---|---|---:|---|
| `id` | int PK | ✅ | |
| `workspace_gid` | text | ✅ | Asana workspace |
| `asana_gid` | text | ✅ | unique per workspace |
| `email` | text | ✅ | lowercase untuk matching |
| `name` | text | ✅ | |
| `photo_url` | text | ❌ | |
| `synced_at` | datetime | ✅ | |

### `asana_projects`

| Field | Type | Required | Validasi/Notes |
|---|---|---:|---|
| `id` | int PK | ✅ | |
| `project_gid` | text, unique | ✅ | |
| `name` | text | ✅ | |
| `archived` | bool | ✅ | default false |
| `ai_enabled` | bool | ✅ | default false — gating AI pilih project |
| `keywords` | text | ❌ | bantu matching |
| `ai_notes` | text | ❌ | instruksi tambahan buat AI |
| `synced_at` | datetime | ✅ | |

### `user_integrations`

| Field | Type | Required | Validasi/Notes |
|---|---|---:|---|
| `id` | int PK | ✅ | |
| `user_id` | FK hub users | ✅ | |
| `provider` | text | ✅ | `asana` (v1; struktur siap provider lain) |
| `external_id` | text | ✅ | gid Asana user |
| `provider_email` | text | ❌ | |
| `provider_name` | text | ❌ | |
| `status` | enum auto/manual | ✅ | auto = hasil email match; manual = set admin/MCP |

Unique: (`user_id`, `provider`). Ini tabel yang ikut dimigrasi dari Nekorp (open question #3).

## 3. RBAC

| Action | member | admin |
|---|:-:|:-:|
| Lihat projects (yang `ai_enabled`) | ✅ | ✅ |
| Trigger sync cache users/projects | ❌ | ✅ |
| Set/ubah identity mapping sendiri | ❌ | ✅ (member lihat sendiri) |
| Create/complete task | ✅ (via MCP atau UI) | ✅ |

## 4. Routes & API

| Method | Path | Desc |
|---|---|---|
| GET | `/api/nekowork/asana/projects` | Daftar project (filter `ai_enabled=true` untuk konsumsi AI) |
| POST | `/api/nekowork/asana/tasks` | Create task: `{name, notes?, assignee_user?, project_gid?, due_on?}` |
| PUT | `/api/nekowork/asana/tasks/{gid}` | Update/complete task |
| POST | `/api/nekowork/asana/sync` | Admin: refresh cache users + projects |
| GET | `/api/nekowork/identities` / POST / DELETE | Kelola `user_integrations` |

## 5. Integrasi Maton

- Auth: `MATON_API_KEY` (NekoVault `api/maton`), connection Asana via `MATON_ASANA_CONNECTION_ID` (env, sama kayak Nekorp).
- Create task → Maton `POST /tasks`; complete → Maton `PUT /tasks/{gid}`.
- Output ringkas saja (id, nama, url) — tanpa payload mentah.
- Fallback n8n dari Nekorp **gak diport** — Maton langsung.

## 6. Business Rules

1. **Assignee resolver:** input `assignee_user` (user Hub) → lookup `user_integrations` provider `asana` → gid. Kalau belum ada mapping: auto-match email lowercase ke `asana_users_cache`; kalau match tunggal → pakai + catat `status=auto`; kalau ambigu/gak ada → gagal dengan daftar kandidat, gak asal pilih.
2. AI (agent Nekowork) cuma boleh pilih project `archived=false AND ai_enabled=true`, dibantu `name + keywords + ai_notes`. Manusia bebas pilih semua project.
3. Cache bukan source of truth keberadaan task — kegagalan upstream = error jelas, bukan hasil kosong palsu.
4. Sync cache idempotent (upsert by gid).

## 7. Definition of Done

- [ ] Create task end-to-end via Maton mendarat di Asana (task real di project uji).
- [ ] Complete/uncomplete task jalan.
- [ ] Assignee resolve benar: mapping manual → auto email match → gagal jelas dengan kandidat.
- [ ] AI gating: agent cuma lihat project `ai_enabled`.
- [ ] Sync cache admin jalan, idempotent, tanpa duplikat.
- [ ] Semua write kecatat di audit log Hub.
