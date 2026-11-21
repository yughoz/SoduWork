# Modul: Dashboard Sisa Token

> User lihat sisa saldo & pemakaian token New API-nya — di admin console dan di desktop client (sebagai app).

---

## 1. Tujuan

Port dashboard usage Nekorp (newapi-dashboard, usage harian per token) ke Nekowork, dengan permintaan khusus: **muncul juga di client** (Electron), bukan cuma console web.

Data diambil live dari New API (quota + logs). Gak ada tabel duplikasi saldo — caching ringan boleh, source of truth tetap New API.

## 2. Konten UI

Per user (halaman `/tokens` + app client):

```text
┌─ Token LLM ────────────────────────────────────────┐
│ Status: aktif    nk-****...****   [Reset] [Revoke] │
│                                                    │
│ ┌──────────────┐ ┌──────────────┐ ┌─────────────┐  │
│ │ Saldo        │ │ Pakai hari   │ │ Pakai bulan │  │
│ │ $7.42        │ │ $0.85        │ │ $12.30      │  │
│ └──────────────┘ └──────────────┘ └─────────────┘  │
│                                                    │
│ Riwayat 14 hari (bar per hari, USD)                │
│ ▂▅▃▇▁▂▆▃▁▂...                                     │
└────────────────────────────────────────────────────┘
```

Admin (console, `/nekowork/dashboard`): summary semua user — total saldo, siapa paling besar pemakaian bulan ini, token disabled/kosong, sinkron gagal.

## 3. Sumber Data

| Angka | Sumber |
|---|---|
| Saldo sisa | New API token `quota` → `quotaToUsd` (quotaPerUnit 500000) |
| Pakai per hari | New API logs per token name, summary per tanggal (port `summarizeNewApiTokenUsageForDate`) |
| Pakai bulan | agregat harian |
| Riwayat 14 hari | deret harian, cache 5 menit di Hub biar gak nembak API |

## 4. Routes & API

| Method | Path | Desc |
|---|---|---|
| GET | `/api/nekowork/usage/me` | Saldo + usage user sendiri (dipakai console & client app) |
| GET | `/api/nekowork/usage` | Admin: ringkasan semua user |

Client app: satu app Nekowork sederhana yang render endpoint `/usage/me` di panel app client (mekanisme app Nekowork — detail packaging menyusul, catatan implementasi).

## 5. RBAC

| Action | member | admin |
|---|:-:|:-:|
| Lihat saldo/usage sendiri | ✅ | ✅ |
| Lihat usage semua user | ❌ | ✅ |

## 6. Business Rules

1. Angka USD konsisten dengan dashboard New API (konversi quota sama).
2. Cache 5 menit; tombol refresh manual mem-bypass.
3. User tanpa token masih lihat halaman dengan state "belum ada token" + tombol generate (modul_gateway).
4. Gak ada penyimpanan saldo historis di v1 — riwayat dibangun dari logs New API. Kalau butuh snapshot permanen, jadi modul sendiri nanti.

## 7. Definition of Done

- [ ] Saldo user cocok dengan angka di dashboard New API.
- [ ] Riwayat 14 hari kebentuk dari logs, cache jalan (gak ada spam hit ke New API).
- [ ] Halaman tampil di admin console dan di desktop client.
- [ ] State user tanpa token jelas + terhubung ke generate.
- [ ] Admin summary semua user jalan.
