# Model yang tersedia di zanslab (21), dan yang DIPAKAI

Dicatat supaya tidak terulang: aku pernah menambahkan model zanslab di luar
maksud pemilik produk, dan itu salah. **zanslab dipakai HANYA untuk dua model.**

## Yang DIPAKAI (2)

| Model | Harga output | Alasan |
|---|---|---|
| `zanslab/op/gpt-6-sol` | Rp8.500 / 1M | kualitas |
| `zanslab/op/gpt-6-luna` | Rp800 / 1M | termurah |

## Yang TERSEDIA tapi TIDAK dipakai (19)

Jangan tambahkan tanpa izin pemilik produk. Kalau butuh salah satu, tanya dulu.

| Model | Catatan pengukuran (2026-10-03) |
|---|---|
| `zanslab/an/claude-sonnet-5` | 4.2s — tercepat, jawaban paling tajam |
| `zanslab/an/claude-opus-5.5` | 8.2s — Opus generasi terbaru, 1M context |
| `zanslab/an/claude-opus-5` | belum diukur |
| `zanslab/an/claude-opus-4-8` | 8.4s |
| `zanslab/an/claude-opus-4-6-thinking` | belum diukur |
| `zanslab/an/claude-sonnet-4-6` | belum diukur |
| `zanslab/op/gpt-6-astra` | **503 — tidak tersedia** |
| `zanslab/op/gpt-5.6-sol` | belum diukur |
| `zanslab/op/gpt-5.6-sol-review` | belum diukur |
| `zanslab/op/gpt-5.6-terra` | belum diukur |
| `zanslab/op/gpt-5.6-terra-review` | belum diukur |
| `zanslab/op/gpt-5.6-luna` | belum diukur |
| `zanslab/op/gpt-5.6-luna-review` | belum diukur |
| `zanslab/go/gemini-3.8-flash-high` | 10.8s, 929 reasoning token |
| `zanslab/go/gemini-3.7-flash-high` | belum diukur |
| `zanslab/go/gemini-pro-agent` | belum diukur |
| `zanslab/de/deepseek-v4.1-flash(max)` | **503 — tidak tersedia** |
| `zanslab/de/deepseek-v4-flash-vision-exp` | belum diukur |
| `zanslab/gpt-6-astra` | belum diukur (nama tanpa prefix vendor) |

## Fakta penting tentang zanslab

- **Tidak memblokir prompt bahasa Indonesia.** Ini keunggulan utamanya dibanding
  AgentRouter, yang mengembalikan `400 content-blocked` untuk prompt Indonesia.
- Latensi 1.3-4.2s — jauh lebih cepat dari AgentRouter (TTFT 20-56s).
- API key pernah salah isi (baseUrl masuk ke field apiKey) dan menghasilkan
  `401 INVALID_API_KEY`. Sudah diperbaiki pemilik produk.
