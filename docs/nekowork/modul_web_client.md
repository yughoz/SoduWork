# PRD Nekowork — Web Client (kerja dari mana aja via Runner desktop)

- Versi: 0.1 (draft — menunggu konfirmasi scope)
- Last updated: 2026-11-21
- Owner: Kamisama / Nekorp
- Basis riset: repo `~/Projects/SoduWork` (cloud-conversations, account-conversations, runner_ws, remote_screen)
- Kaneo: project NW

## 1. Masalah

Client Nekowork = desktop only (Electron). Chat/eksekusi agent cuma bisa dari PC yang install client. User mau bisa **kerja dari mana aja** (HP/laptop lain/browser) tanpa bawa PC, tapi eksekusi tetap di PC sendiri yang sudah install desktop client.

Upstream roadmap: "Web and mobile apps — In progress" (belum rilis). Kita gak mau nunggu.

## 2. Solusi (konsep)

Browser = front-end tipis. Eksekusi tetap lokal:

```text
[Browser: HP/laptop mana pun]
   |  https://nekowork.nekoding.xyz  (sudah publik via tunnel)
   v
[Nekowork Hub :18989]  ── transkrip, auth, dispatch, approval (sudah ada)
   |  WebSocket jobs (sudah ada)
   v
[RunnerNode: PC user]  ← desktop client jalan = runner online (sudah ada)
   └─ hermes / codex / pi eksekusi lokal, hasil stream balik ke Hub → browser
```

Prinsip: **Hub = satu-satunya yang harus publik. Runner PC connect outbound ke Hub (sudah begini), jadi gak ada port/tunnel baru.** Desktop client tetap wajib terinstall & jalan di PC — dia adalah eksekutor.

## 3. Yang sudah ada (hasil riset — gak perlu dibangun ulang)

| Primitif | Lokasi | Catatan |
|---|---|---|
| Registrasi & dispatch ke RunnerNode | `app/api/runner_ws.py` | handshake runner_id/runner_key, heartbeat, job by-ID, event replay |
| Conversation di remote Runner | `app/api/cloud_conversations.py` | "create a conversation directly on an authorized remote Runner" — include PC runner kalau online |
| Transkrip tersimpan di Hub | `app/api/account_conversations.py` | directory + messages + sync; metadata-only listing |
| Handoff antar device | `app/services/conversation_handoffs.py` | "cloud handoff moves context, not processes" |
| Realtime WS | `app/api/ws.py` + `app/realtime.py` | device session auth |
| Access policy mode | `app/services/acp_access.py` | standard/readonly/full → mode native per harness (hermes baru ditambah) |
| Remote screen (read-only) | `app/api/remote_screen.py` | bonus: lihat layar PC dari jauh, tanpa input |

Yang **belum ada**: browser UI untuk chat/workbench + auth browser sebagai account user + relaying approval ke browser.

## 4. Modul

### M1 — Web Auth (browser session)
- Login pakai akun Hub yang sudah ada (username/password, sama dengan desktop) di `nekowork.nekoding.xyz`.
- Session browser = token HttpOnly cookie, lifetime pendek + refresh. **Bukan** device auth (browser bukan device, tidak jadi runner).
- Endpoint web berjajar di `/api/v1/web/*`, guard terpisah dari admin console (role `user.manage` dll gak otomatis).

### M2 — Workbench SPA (chat UI)
- SPA baru di Hub (satu halaman `/app`), bahasa Indonesia, gaya konsol admin (reuse komponen gaya hub-frontend, bukan copy Electron).
- Fitur MVP: daftar conversation (account-scoped), chat box, stream balasan (WS), status runner (online/offline), indikator mode akses.
- Gak ada di MVP: issue workflow, deliverables library, plugins, schedules — desktop tetap tempat fitur lengkap.

### M3 — Dispatch & streaming
- Buat conversation → pilih target runner = **PC milik sendiri yang online** (v1: hanya runner milik user login; runner orang lain gak bisa dipilih).
- Reuse pipeline `cloud_conversations` + `runner_ws` (job dispatch by-ID, event replay saat reconnect).
- Stream ke browser: WebSocket `/api/v1/web/ws` (cookie auth), fan-out dari event task yang sama.

### M4 — Approval di web
- Permission mode dibatasi v1: **standard saja** (file write di workspace + tanya untuk sensitif). Mode `full` dari web = ditolak dengan pesan jelas; `readonly` menyusul.
- Approval prompt (edit file di luar workspace, command berisiko) muncul inline di chat browser → jawaban diteruskan ke runner via pipeline approval yang sama.
- Audit: tiap aksi web tercatat `web.*` di audit log hub.

### M5 — Keamanan
- Web tidak pernah kirim command shell langsung; semua lewat conversation pipeline yang sama seperti desktop.
- Workspace tetap folder di PC. File akses dari web = via conversation (bukan file manager).
- Rate limit + CSRF (SameSite cookie) + logout semua sesi.
- Kalau PC offline: conversation tetap bisa dibuat, jadi antre; tampil status "menunggu runner online".

## 5. Di luar scope v1
- Mobile native app; push notification.
- Eksekusi di cloud runner (VPS) — arsitektur mendukung, dibuka belakangan.
- Read-only mode & full mode dari web.
- Upload file besar dari browser (v1: file via PC/workspace).

## 6. Acceptance
1. Dari HP browser: login → lihat conversation → kirim prompt → Hermes/Codex jalan di PC → balasan stream ke HP.
2. Approval inline muncul & kejawab dari HP, tercatat di audit.
3. PC mati → conversation antre dengan status jelas; PC nyala lagi → lanjut.
4. User lain gak bisa pilih runner milik orang; endpoint web gak bocor ke admin console guard.
