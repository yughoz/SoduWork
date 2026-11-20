# Modul Titen — Memory Server Embedded

## Tujuan

Tiap user Nekowork dapat memory Titen **pribadi** yang jalan **di dalam Nekowork** (bukan instance `nebula` yang dipakai agent-agent internal). Tiga requirement:

1. **Server embedded** — `titen serve` dikelola Hub, DB sendiri, bukan instance eksternal.
2. **Titen management** — admin bisa lihat status, mint/rotate/revoke key per user.
3. **Client auto-setup** — setelah login, desktop client sudah punya koneksi Titen terkonfigurasi pakai key user. User gak ngapa-ngapain.

## Keputusan desain

| Keputusan | Nilai | Alasan |
|---|---|---|
| Topologi | **Satu** `titen serve` child process milik Hub, DB `titen.db` di data dir Hub | Titen multi-user native (org/workspace/project + authz pre-retrieval); N DB terpisah gak perlu |
| Isolasi user | 1 project Titen per user (auto-create saat mint key) + key dengan fence `--projects <project-user>` | `read_graph` baca semua yang diizinkan — fence mencegah user lihat memory user lain. Project shared `org` opsional belakangan |
| Runtime | Bun + `titen-memory` (installer `titen.dev`), di-setup **manual sekali** di host Hub | Hub gak auto-download script; kalau belum ada, status modul = `unavailable` + panduan setup |
| Port | `127.0.0.1:18787` (default, konfigurable) — loopback, gak di-expose | Satu ingress tetap Hub :18989 |
| Akses client | **Proxy Hub** `GET/POST /api/v1/titen/mcp` → forward JSON-RPC ke titen serve, header `Authorization: Bearer` di-pass-through | Client cuma kenal Hub; validasi key tetap di Titen (hash-only di Hub) |
| Penyimpanan key | Hub simpan **hash + key_id + mapping user** aja; plaintext key dikirim sekali ke device via personal MCP entry (kolom `Client.mcp_servers`, headers write-only upstream) | Ikuti pola personal MCP upstream; Hub gak nyimpan plaintext |
| Scopes default | `mcp:call,projects:resolve,observations:write,claims:write,context:compile,feedback:write,evidence:read`, `--trust asserted` | Standar per-agent dari skill titen-ops |
| Bootstrap | Org `Nekowork`, idempotent saat pertama start; owner key bootstrap → NekoVault, bukan chat/log | `serve` nolak DB kosong |

## Data model

Tabel `nekowork_titen_key` (Alembic baru, revisi setelah `0093_newapi_accounts`):

| Kolom | Tipe | Catatan |
|---|---|---|
| id | String(36) PK | uuid |
| user_id | String unique index | 1 key aktif per user |
| project_id | String | project Titen milik user |
| key_id | String | `key_...` dari Titen |
| key_hash | String(64) | SHA-256 hex, buat verifikasi pass-through |
| status | Enum `active/revoked` | |
| created_at / revoked_at | DateTime | |

Settings baru: `titen_enabled` (bool, default true), `titen_base_url` (default `http://127.0.0.1:18787`), `titen_db_path` (default `<data dir>/titen.db`), `titen_org_name` (`Nekowork`).

## API

Admin (`/api/v1/titen/...`, role super admin):

| Endpoint | Fungsi |
|---|---|
| `GET /status` | server up/down, org id, jumlah key aktif, bun/titen terpasang |
| `GET /keys` | semua key per user (masked, tanpa plaintext) |
| `POST /keys/{user_id}` | mint / rotate (revoke lama + mint baru) + auto-create project user |
| `DELETE /keys/{user_id}` | revoke key + hapus entry personal MCP di device user |

User (member, data sendiri):

| Endpoint | Fungsi |
|---|---|
| `GET /me` | status: project id, key ada, url proxy, masked key |
| `POST /me/rotate` | mint key baru (show-once sekali, untuk pemakaian manual/CLI) |
| `POST|GET /mcp` | proxy JSON-RPC pass-through ke titen serve (auth hub → forward + Bearer) |

## Alur client auto-setup

1. User login di desktop client → device sync (mekanisme personal MCP upstream).
2. Hub mastiin user punya key aktif (auto-mint pertama kali: create project user + `titen key create` + capture plaintext via redirect file di host, plaintext gak masuk log).
3. Hub inject/update entry personal MCP `{name: "titen", type: "http", url: "<hub>/api/v1/titen/mcp", headers: {Authorization: "Bearer titen_sk_..."}}` ke `Client.mcp_servers` tiap device user — headers write-only, gak kebaca balik.
4. Harness/agent di client langsung punya tools `titen_*` tanpa config manual. Tasks yang jalan di Hub juga ke-inject entry yang sama ("only into the owner's tasks").
5. Rotate/revoke → entry device di-update/dihapus di sync berikutnya.

## Keamanan

- Fence per project di key (bukan cuma konvensi nama subject).
- Proxy validasi sesi hub dulu; header Bearer di-forward utuh; respon error Titen (401/403) di-lewatkan apa adanya.
- Plaintext key: cuma muncul (a) di capture file 0600 sementara pas mint, (b) show-once `/me/rotate`, (c) kolom headers device. Gak pernah di log/chat.
- Audit event `nekowork.titen.mint/rotate/revoke/proxy_denied`.
- Revoke semua key user pas user dinonaktifkan.

## Test plan

- Unit: mint/rotate/revoke + hash mapping; unique per user; fence flag benar saat invoke `titen key create` (argv di-mock).
- Integration: proxy pass-through (mock titen serve): header diteruskan, 401 Titen → 401 ke client, sesi hub gak valid → 401 tanpa nyentuh Titen.
- E2E (host dengan titen terpasang): `initialize` JSON-RPC via proxy → 200 `serverInfo`; negative: key user A coba baca project user B → kosong/ditolak.

## Out of scope modul ini

- Project shared antar user (org-wide) + visibility default — belakangan kalau diminta.
- Auto-install bun/titen dari Hub (setup manual, terdokumentasi).
- Dashboard/inspeksi isi graph di console (pakai CLI/dashboard Titen sendiri kalau perlu).
