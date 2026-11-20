# Modul: MCP Nekowork (`/mcp`)

> Port Super MCP Nekorp (`corp.nekoding.xyz/api/mcp`) jadi endpoint MCP di Hub SoduWork — konsumen lama (Hermes, Maus, Cursor) pindah tanpa ubah pola pemakaian.

---

## 1. Tujuan

Agent eksternal connect ke Hub dengan Bearer token dan memakai tools: mapping karyawan (`user_integrations`), task Asana. Nekorp pakai `@modelcontextprotocol/sdk` di Next.js; di Nekowork endpoint hidup di **FastAPI Hub** (streamable HTTP), satu proses dengan Hub.

## 2. Transport & Auth

- Streamable HTTP di route `/mcp` (python `mcp` SDK, runtime sama dengan Hub).
- Auth: `Authorization: Bearer <token>` → SHA-256 lookup `mcp_tokens.token_hash`, `enabled=true`. Gagal → 401 sebelum transport.
- Rate limit 60 req/menit per token (port dari Nekorp).

### `mcp_tokens`

| Field | Type | Required | Validasi/Notes |
|---|---|---:|---|
| `id` | int PK | ✅ | |
| `name` | text, unique | ✅ | nama agent buat audit (`hermes-lele`, `maus`, ...) |
| `token_hash` | text, unique | ✅ | SHA-256 hex |
| `enabled` | bool | ✅ | revoke = false |
| `last_used_at` | datetime | ✅ | update tiap request |
| `created_at` | datetime | ✅ | |

Plaintext cuma saat pembuatan → diserahkan ke user + disimpan NekoVault. Gak ada kolom `corporate` (single-org).

## 3. Tools (6 dari 8 Nekorp)

### Read-only

| Tool | Input | Sumber |
|---|---|---|
| `list_employees` | `{provider?}` | users Hub + `user_integrations` |
| `search_tasks` | `{query?, assignee?, completed?}` | cache task Asana |

### Write

| Tool | Input | Eksekusi |
|---|---|---|
| `set_identity` | `{user, provider, external_id, provider_email?, provider_name?}` | upsert `user_integrations` (status manual) |
| `delete_identity` | `{user, provider}` | hapus row `user_integrations` |
| `create_asana_task` | `{name, notes?, assignee_user?, project_gid?, due_on?}` | Maton POST `/tasks` (modul_asana) |
| `complete_asana_task` | `{task_gid, completed}` | Maton PUT `/tasks/{gid}` |

`repo_activity` & `daily_digest` (GitLab) **gak ikut v1** — GitLab sync gak diport. Contract input/output tool dibuat identik dengan Nekorp biar client lama gak perlu ubah prompt/配置.

## 4. Audit & Error

- Tiap `tools/call` → audit log Hub, actor `mcp:<name>`, metadata = arguments (tanpa output/secret).
- Token invalid → 401 JSON. Tool gagal → `isError: true` + pesan singkat tanpa stack.
- Assignee resolution & gating project pakai aturan modul_asana apa adanya.

## 5. RBAC

| Action | member | admin |
|---|:-:|:-:|
| Pakai tools via token sendiri | ✅ | ✅ |
| Create/revoke `mcp_tokens` | ❌ | ✅ |
| Lihat `last_used_at` semua token | ❌ | ✅ |

## 6. Migration dari Nekorp

1. Generate token baru di Nekowork untuk tiap agent aktif (Hermes, Maus) → simpan NekoVault.
2. Repoint client (`corp.nekoding.xyz/api/mcp` → `<hub>/mcp`) satu per satu, verify handshake + tools/list + 1 tools/call.
3. Token lama Nekorp di-disable setelah semua pindah.

## 7. Definition of Done

- [ ] Handshake MCP Inspector lulus (initialize, tools/list, tools/call).
- [ ] Tanpa token / token disabled → 401; 6 tools balikin data benar.
- [ ] Write tools jalan e2e (task Asana real) + ter-audit dengan actor `mcp:<name>`.
- [ ] Rate limit aktif (60 req/menit/token).
- [ ] Hermes & Maus repoint ke Nekowork dan tetap jalan dengan prompt lama.
