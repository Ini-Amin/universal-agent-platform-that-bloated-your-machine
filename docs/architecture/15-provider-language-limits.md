# Batasan Provider: Bahasa & Filter

Ditemukan lewat percobaan langsung (2026-10-03). Ini bukan opini — setiap baris
punya bukti request/response.

## Ringkasan

| Provider | Prefix | Bahasa Indonesia | Catatan |
|---|---|---|---|
| **zanslab** | `zanslab/` | ✅ **boleh** | 1.0-2.7s. Prioritas utama |
| **Antigravity** | `ag/` | ✅ **boleh** | ~13s |
| **vsllm** | `vs/` | ✅ boleh | 9-37s |
| **AgentRouter** | `AG/` | ❌ **DILARANG** | filter sensitive word |
| cbai, dahl, oc, ps, cl, jdw | — | ✅ boleh | cadangan |

## AgentRouter: JANGAN pakai bahasa Indonesia

Pemilik produk memberi tahu langsung: **"Agentrouter punya sensitive word,
jangan pakai bahasa indonesia namun bahasa inggris atau china"**.

Bukti terukur — prompt yang sama, tiga model, satu bahasa:

```
Prompt INGGRIS   "Reply with exactly: hello world"
  AG/claude-opus-4-8  -> ✅ OK
  AG/claude-opus-5    -> ✅ OK
  AG/gpt-6-astra      -> ✅ OK

Prompt INDONESIA "Balas dengan tepat: halo dunia"
  AG/claude-opus-4-8  -> ❌ HTTP 400 content-blocked
  AG/claude-opus-5    -> ❌ HTTP 400 content-blocked
  AG/gpt-6-astra      -> ❌ HTTP 400 content-blocked
  AG/deepseek-v4-flash-> ❌ HTTP 400 content-blocked
```

**Pola:** konsisten di 4 model. Bukan masalah model, bukan kuota — filter di sisi
AgentRouter.

### Aturan pakai AgentRouter

1. **Prompt HARUS bahasa Inggris atau Mandarin.** Jangan Indonesia.
2. Kalau prompt dari user berbahasa Indonesia, terjemahkan dulu ke Inggris,
   kirim ke AgentRouter, lalu terjemahkan hasilnya kembali.
3. Karena aturan ini menyusahkan, `ag/` (Antigravity) dan `zanslab/` ditaruh
   **di depan** chain — keduanya menerima bahasa Indonesia.

## Status AgentRouter (2026-10-03)

Dashboard bilang **7/7 model tersedia, success rate 96.52%**. Kenyataannya:

```
✅ AG/deepseek-v4-flash       0.7s   OK   (tapi blokir bahasa Indonesia)
❌ AG/gpt-6-astra             503
❌ AG/claude-opus-4-8         503
❌ AG/claude-opus-5           503
❌ AG/claude-sonnet-4-6       503
❌ AG/gemini-3.8-flash-high   503
❌ AG/deepseek-v4-pro         503
```

**1 dari 7 benar-benar jalan.** Dashboard tidak mencerminkan kondisi nyata.
`claude-opus-4-8` yang tadinya "Normal" sekarang 503.

**Kesimpulan:** AgentRouter praktis tidak bisa diandalkan. Tetap ada di chain
sebagai cadangan, tapi jangan berharap banyak.

## Catatan tentang zanslab

Punya kredit gratis hampir utuh (per screenshot pemilik produk):
- **Free Credit GPT**: Rp 5,68 / Rp 5.000 terpakai (0%), berlaku s/d 4 Okt 2026
- **Free Credit DeepSeek**: Rp 0 / Rp 10.000 terpakai, **reset harian**

Model yang hidup (diukur): `op/gpt-6-sol` (2.1s), `op/gpt-6-luna` (1.7s),
`op/gpt-5.6-sol` (2.3s), `op/gpt-5.6-luna` (2.7s),
`de/deepseek-v4-flash-vision-exp` (1.0s).

Model yang 503: `op/gpt-6-astra`, `de/deepseek-v4.1-flash(max)`.

**Batasan pemilik produk:** zanslab dipakai **HANYA untuk `op/gpt-6-sol` dan
`op/gpt-6-luna`**. Jangan tambahkan model zanslab lain tanpa izin.

## Masalah cache OMP (akar masalah "model tidak terpakai")

Model di chain **tidak terpakai** bukan karena config salah, tapi karena cache:

```
~/.omp/agent/models.db -> model_cache -> provider_id
  '9router:openai-models-list-context-v3'

Cache ditulis : 2026-10-02 17:22
zanslab dibuat: 2026-10-03 03:54   <- 10 jam SETELAH cache
```

OMP log: `runSubagent: reusing parent modelRegistry; skipping refresh`.

Jadi entri chain yang menunjuk model baru **tidak bisa di-resolve** dan
dilewati. Perbaikan: hapus baris `9router%` dari `model_cache` supaya OMP fetch
ulang. Backup: `~/.omp/agent/models.db.backup-<timestamp>`.

**Kalau menambah provider/model baru di 9router, hapus cache OMP-nya.**
Kalau tidak, model itu tidak akan pernah dipakai oleh subagent.
