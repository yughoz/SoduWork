# Modul: Gateway New API (llm.nekotekno)

> Tiap user Nekowork punya 1 token New API self-service; provisioning user gateway otomatis di belakang layar.

---

## 1. Tujuan

User bisa generate/reset/revoke token LLM sendiri tanpa approval admin, konek ke `llm.nekotekno.xyz` (New API). Ini port alur `src/lib/newapi.ts` + `modul_newapi_token_lifecycle.md` Nekorp ke Hub Nekowork:

- Self-service, show-once, bisa diaudit admin.
- Nama/token konsisten biar migrasi dari token Nekorp (`nekorp-{email}`) gak bikin duplikat.
- Saldo (quota) dibaca live dari New API buat modul dashboard.

## 2. Field Spesifikasi

Tabel lokal Hub (Alembic) — **gak ada duplikasi data token; token asli tinggal di New API**:

### `newapi_accounts`

| Field | Type | Required | Validasi/Notes |
|---|---|---:|---|
| `id` | int PK | ✅ | |
| `user_id` | FK hub users, unique | ✅ | 1 akun gateway per user Hub |
| `newapi_user_id` | int | ✅ | id user di New API |
| `username` | text, unique | ✅ | `nekowork_{user_id}` (deterministik, port `getNekorpNewApiUsername`) |
| `token_id` | int, nullable | ❌ | id token aktif di New API; null = belum ada token |
| `token_name` | text, nullable | ❌ | `nekowork-{email}` |
| `token_key_masked` | text, nullable | ❌ | `nk-****...****`, tampil setelah show-once |
| `status` | enum active/disabled | ✅ | mirror status token |
| `created_at` / `updated_at` | datetime | ✅ | |

Plaintext token **gak pernah disimpan** — diambil dari `getTokenFullKey` saat create lalu ditampilkan sekali ke user.

## 3. RBAC

| Action | member | admin |
|---|:-:|:-:|
| Generate/reset/revoke token sendiri | ✅ | ✅ |
| Lihat masked key + saldo sendiri | ✅ | ✅ |
| Lihat status token semua user | ❌ | ✅ |
| Revoke token user lain | ❌ | ✅ |
| Lihat plaintext token siapa pun | ❌ | ❌ |

## 4. Routes & API

UI (admin console + client app):

- `/tokens` — halaman token user sendiri (status, masked key, tombol generate/reset/revoke, saldo).

REST (prefix `/api/nekowork`):

| Method | Path | Desc |
|---|---|---|
| POST | `/tokens` | Generate token (provision user New API dulu kalau belum ada) |
| POST | `/tokens/reset` | Revoke lama + generate baru |
| DELETE | `/tokens` | Revoke token aktif |
| GET | `/tokens/me` | Status akun: token_id, masked, saldo live, status |
| GET | `/tokens` | Admin: daftar token semua user |

Semua endpoint nulis audit log Hub (actor = user).

## 5. Integrasi New API (port dari Nekorp)

- **Management auth:** header dari admin access token + user id (`buildNewApiManagementHeaders` pattern) — kredensial dari NekoVault `api/nekotekno`, gak pernah hardcode.
- **Provisioning user gateway:** `createNewApiUser` dengan username/password deterministik dari user id Hub.
- **Token CRUD:** `createToken` / `deleteToken` / `findTokenByCandidateNames`.
- **Konversi quota:** `quotaToUsd` / `usdToQuota`, `quotaPerUnit = 500000`.
- **Token naming:** baru = `nekowork-{email}`; lookup fallback candidates `nekowork-{email}`, `nekorp-{email}`, `nekorp-{userId}` biar token lama Nekorp kebaca.
- **Model:** group default dari config (open question #4); model `nekolite`/`nekopro` tetap berlaku.

## 6. Business Rules

1. 1 akun `newapi_accounts` per user Hub (unique `user_id`), 1 token aktif per akun.
2. Reset = delete token lama di New API + create baru; plaintext baru tampil sekali.
3. Generate pertama kali = provision user gateway (idempotent kalau username udah ada).
4. Revoke → `token_id`/`token_name`/`masked` di-null-kan, `status` tetap `active` (akun tetap ada).
5. Kegagalan API New API → error jelas ke user, gak ada state setengah jadi: provisioning sukses tapi token gagal tetap simpan akun, user tinggal retry generate.
6. Admin gak bisa liat plaintext, cuma masked + status.

## 7. Definition of Done

- [ ] User baru bisa generate token sendiri dari UI, plaintext tampil sekali, setelah itu masked.
- [ ] Reset & revoke jalan; token lama benar-benar invalid di New API.
- [ ] User Nekorp lama (token `nekorp-{email}`) kebaca via candidates tanpa duplikat.
- [ ] Saldo live (USD) balikin angka yang cocok dengan dashboard New API.
- [ ] Admin lihat semua token, bisa revoke; plaintext gak pernah muncul di UI/API admin.
- [ ] Audit log kecatat untuk generate/reset/revoke.
