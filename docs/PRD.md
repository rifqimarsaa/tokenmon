# Product Requirements Document: Tokenmon

## 1. Ringkasan

Tokenmon adalah alat internal berbasis command-line interface (CLI) untuk memantau penggunaan Claude Code dan Codex pada beberapa perangkat yang menggunakan akun langganan yang sama. Versi awal ditujukan untuk satu pengguna, maksimal tiga perangkat, dengan Claude Max dan Codex Pro.

Tokenmon menggabungkan dua jenis informasi yang harus selalu dibedakan:

1. **Atribusi lokal:** jumlah token dan aktivitas yang teramati pada setiap perangkat, workspace, provider, dan sesi.
2. **Allowance akun:** sisa kapasitas dan waktu reset yang ditampilkan secara resmi oleh Claude atau Codex, jika dapat diakses dengan aman.

Tokenmon tidak menganggap jumlah token lokal sebagai ukuran langsung persentase allowance langganan karena konsumsi allowance dapat dipengaruhi model, reasoning, tool, panjang konteks, caching, dan kebijakan provider.

## 2. Latar Belakang

Satu akun Claude Max dan satu akun Codex Pro digunakan pada hingga tiga perangkat melalui CLI dan VS Code. Provider menampilkan penggunaan pada level akun, sehingga pengguna tidak dapat melihat perangkat atau workspace mana yang paling banyak menggunakan kapasitas.

Pengguna membutuhkan satu perintah CLI untuk melihat penggunaan gabungan dan pembagiannya tanpa mengirim prompt, respons, atau source code ke sistem monitoring.

## 3. Tujuan

- Menampilkan penggunaan Claude Code dan Codex per perangkat.
- Menampilkan penggunaan per workspace, provider, model, dan sesi.
- Menggabungkan data dari maksimal tiga perangkat.
- Menampilkan jumlah input, output, cache-read, dan cache-write token ketika tersedia.
- Menampilkan bagian penggunaan lokal setiap perangkat dalam persentase.
- Menampilkan allowance resmi akun dan waktu reset ketika sumber yang stabil tersedia.
- Beroperasi melalui CLI setelah instalasi.
- Menjaga data sensitif tetap berada di perangkat pengguna.

## 4. Bukan Tujuan

- Menghitung tagihan API Claude atau OpenAI.
- Mengubah atau mengakali batas Claude Max maupun Codex Pro.
- Mengklaim persentase allowance berdasarkan token lokal.
- Menyimpan isi prompt, respons model, source code, diff, atau nama file yang dibaca model.
- Menyediakan dashboard web pada MVP.
- Mendukung banyak pengguna, organisasi, atau role-based access pada MVP.
- Menggantikan antarmuka usage resmi provider.

## 5. Pengguna Sasaran

Satu pengguna teknis yang memakai:

- Claude Max melalui Claude Code CLI dan integrasi VS Code.
- Codex Pro melalui Codex CLI dan integrasi editor yang memakai penyimpanan sesi lokal yang sama.
- Maksimal tiga komputer milik sendiri.

## 6. Istilah

- **Device:** komputer yang telah didaftarkan ke Tokenmon.
- **Workspace:** root repository Git; jika tidak ada, direktori kerja sesi.
- **Client:** jalur penggunaan, seperti CLI atau VS Code.
- **Session:** sesi Claude Code atau Codex yang memiliki identitas unik.
- **Local usage:** token dan aktivitas yang ditemukan collector pada perangkat.
- **Account allowance:** kapasitas tersisa dan waktu reset yang dilaporkan provider.
- **Share:** persentase local usage suatu device atau workspace terhadap total local usage pada rentang waktu yang sama.

## 7. Prinsip Produk

1. Jangan mencampur local usage dengan account allowance.
2. Jangan mengumpulkan konten pengguna.
3. Data yang tidak tersedia harus ditampilkan sebagai `N/A`, bukan diperkirakan tanpa label.
4. Setiap nilai harus memiliki sumber dan waktu pembaruan.
5. Collector tidak boleh mengganggu proses Claude Code, Codex, VS Code, atau terminal.
6. Kegagalan sinkronisasi tidak boleh menghilangkan data lokal.

## 8. Ruang Lingkup MVP

### 8.1 Collector lokal

Collector berjalan di background pada setiap perangkat dan:

- Membuat `device_id` acak saat registrasi pertama.
- Mengambil nama perangkat yang dapat diganti pengguna.
- Mendeteksi sesi baru atau perubahan sesi Claude Code dan Codex.
- Mengambil metrik token yang tersedia dari data lokal atau output terstruktur yang didukung.
- Menentukan workspace dari root Git atau current working directory.
- Mengklasifikasikan client sebagai `vscode`, `cli`, atau `unknown`.
- Menyimpan cursor/checkpoint agar record tidak dihitung dua kali.
- Menulis data ke SQLite lokal sebelum sinkronisasi.
- Mengirim hanya record statistik ke penyimpanan pusat.

### 8.2 CLI monitoring

CLI menyediakan perintah berikut:

```text
tokenmon                  Ringkasan allowance dan penggunaan lokal
tokenmon devices          Penggunaan per perangkat
tokenmon workspaces       Penggunaan per workspace
tokenmon sessions         Daftar sesi dan metriknya
tokenmon today            Penggunaan hari ini
tokenmon week             Penggunaan tujuh hari terakhir
tokenmon watch            Memperbarui tampilan secara berkala
tokenmon sync             Menjalankan sinkronisasi sekarang
tokenmon doctor           Memeriksa collector, sumber data, dan sinkronisasi
tokenmon device rename    Mengganti nama perangkat aktif
```

### 8.3 Tampilan default

```text
TOKENMON
Updated: 2026-10-28 14:20 +07

ACCOUNT ALLOWANCE
Claude Max   5-hour: 72% remaining   Weekly: 48% remaining   Reset: 2h 14m
Codex Pro    Weekly: 63% remaining                              Reset: 4d 08h

LOCAL USAGE - LAST 7 DAYS
Device          Provider   Workspace       Input    Output   Cache    Total   Share
Laptop-office   Claude     project-api      120K       32K     30K     182K     39%
Laptop-home     Claude     project-web       78K       21K     15K     114K     24%
PC-main         Codex      project-api      121K       34K     18K     173K     37%

Local total: 469K tokens
```

Jika allowance resmi tidak tersedia:

```text
Claude Max   Allowance: unavailable   Last checked: 14:20
```

Tokenmon tidak menggantinya dengan estimasi token.

## 9. Persyaratan Fungsional

### FR-1: Registrasi perangkat

- Setiap instalasi menghasilkan `device_id` unik.
- Pengguna dapat memberi nama perangkat.
- Maksimal tiga perangkat aktif pada MVP.
- Registrasi ulang tidak boleh membuat duplikasi tanpa peringatan.

### FR-2: Akuisisi data lokal

- Sistem mendukung Claude Code dan Codex.
- Sistem hanya membaca sumber data yang diperlukan untuk statistik.
- Setiap event memiliki provider, device, workspace, session, timestamp, dan metrik token yang tersedia.
- Parser harus bersifat version-aware.
- Event yang sama harus menghasilkan ID deterministik untuk deduplikasi.

### FR-3: Identifikasi workspace

- Gunakan canonical Git root ketika tersedia.
- Jika tidak tersedia, gunakan canonical current working directory.
- Path workspace tidak dikirim mentah secara default; kirim nama tampilan dan hash path.
- Pengguna dapat menetapkan alias workspace.

### FR-4: Identifikasi client

- Tandai `vscode` jika indikator proses atau environment VS Code tersedia.
- Tandai `cli` jika sesi berasal dari terminal non-VS Code.
- Gunakan `unknown` jika bukti tidak cukup.
- Jangan menebak client hanya dari nama workspace.

### FR-5: Penghitungan token

- Pisahkan `input_tokens`, `output_tokens`, `cache_read_tokens`, dan `cache_write_tokens` jika tersedia.
- Simpan `total_tokens` sebagai nilai turunan, bukan pengganti komponen asli.
- Tandai metrik yang tidak dilaporkan sebagai `null`, bukan nol.
- Hindari menghitung ulang seluruh file sesi pada setiap scan.

### FR-6: Allowance akun

- Tampilkan nilai allowance hanya dari sumber provider yang dapat ditelusuri.
- Simpan provider, jenis allowance, remaining percentage, reset time, timestamp, dan status freshness.
- Tandai data sebagai stale setelah batas waktu yang dapat dikonfigurasi.
- Kegagalan membaca allowance tidak menghentikan local usage tracking.
- Tokenmon tidak menyimpan credential OAuth atau API key provider di database pusat.

### FR-7: Sinkronisasi

- Sinkronisasi bersifat incremental dan idempotent.
- Data lokal tetap tersimpan ketika server tidak dapat dijangkau.
- Retry menggunakan backoff dan batas maksimum.
- Konflik diselesaikan menggunakan event ID dan timestamp, bukan overwrite seluruh dataset.

### FR-8: Agregasi

- Pengguna dapat memfilter berdasarkan rentang waktu, provider, device, workspace, client, model, dan sesi.
- Share dihitung dengan rumus:

```text
share = local_tokens_kelompok / local_tokens_total_pada_filter_yang_sama * 100
```

- Share selalu diberi label `local share`.
- Token Claude dan Codex boleh dijumlahkan untuk volume, tetapi tidak digunakan untuk membandingkan biaya atau bobot allowance.

### FR-9: Diagnostic

`tokenmon doctor` memeriksa:

- Collector berjalan atau tidak.
- Versi Claude Code dan Codex yang terdeteksi.
- Parser yang kompatibel.
- Waktu event terakhir.
- Ukuran antrean sinkronisasi.
- Koneksi ke penyimpanan pusat.
- Freshness allowance.
- Permission file lokal.

Diagnostic tidak mencetak token autentikasi atau isi sesi.

## 10. Model Data Minimum

### Device

```text
id, display_name, platform, created_at, last_seen_at, active
```

### Workspace

```text
id, device_id, display_name, path_hash, git_remote_hash, first_seen_at
```

### Session

```text
id, provider, device_id, workspace_id, client, model, started_at, last_seen_at
```

### Usage event

```text
id, session_id, observed_at, input_tokens, output_tokens,
cache_read_tokens, cache_write_tokens, source_version, synced_at
```

### Allowance snapshot

```text
id, provider, allowance_type, remaining_percent, resets_at,
observed_at, source, status
```

## 11. Persyaratan Nonfungsional

### Privasi dan keamanan

- Tidak menyimpan prompt, respons, source code, diff, command content, atau isi file.
- Path lokal di-hash sebelum sinkronisasi.
- Credential sinkronisasi disimpan menggunakan credential store sistem operasi jika tersedia.
- Komunikasi antardevice harus terenkripsi.
- Log tidak boleh berisi credential atau konten sesi.
- Database pusat hanya menerima data milik pengguna yang terdaftar.

### Kinerja

- Penggunaan CPU idle collector di bawah 1% pada kondisi normal.
- Penggunaan memori target di bawah 100 MB.
- Perintah ringkasan lokal selesai dalam dua detik.
- Collector tidak melakukan polling file agresif; gunakan incremental scan atau filesystem notification bila stabil.

### Keandalan

- Tidak ada kehilangan event ketika offline selama tujuh hari.
- Sinkronisasi ulang tidak membuat duplikasi.
- File sesi parsial atau rusak tidak menghentikan collector.
- Upgrade parser tidak mengubah record lama secara diam-diam.

### Portabilitas

- MVP memprioritaskan sistem operasi yang digunakan pada tiga perangkat pengguna.
- Path dan process detection diisolasi per platform.
- Output CLI tetap konsisten pada PowerShell, terminal VS Code, dan terminal standar.

## 12. Sumber Data dan Strategi Integrasi

Urutan preferensi sumber data:

1. Output terstruktur atau interface resmi yang tersedia secara lokal.
2. Metadata sesi lokal yang terdokumentasi atau dapat divalidasi.
3. Pembacaan output status yang dibatasi dan version-aware.

Tokenmon tidak boleh melakukan man-in-the-middle TLS, mengambil credential browser, atau memodifikasi executable Claude/Codex.

Setiap adapter provider memiliki kontrak internal yang sama:

```text
discover() -> detected versions and sources
scan(cursor) -> normalized usage events and next cursor
allowance() -> optional allowance snapshot
health() -> adapter diagnostics
```

## 13. Kriteria Penerimaan MVP

MVP diterima jika:

1. Tiga perangkat dapat didaftarkan dan dibedakan.
2. Penggunaan Claude Code dan Codex pada masing-masing perangkat muncul dalam CLI.
3. Workspace yang berbeda pada perangkat yang sama dapat dibedakan.
4. Sesi dari VS Code dan CLI diklasifikasikan ketika indikator tersedia.
5. Token tidak dihitung dua kali setelah restart atau sinkronisasi ulang.
6. Mode offline menyimpan data dan menyinkronkannya setelah koneksi pulih.
7. `tokenmon`, `devices`, `workspaces`, `week`, `sync`, dan `doctor` berfungsi.
8. Tidak ada prompt, respons, source code, atau credential dalam database pusat dan log.
9. Allowance yang tidak tersedia ditampilkan sebagai unavailable, bukan hasil perkiraan.
10. Perbedaan agregat token terhadap data sumber lokal berada dalam toleransi 1% untuk fixture yang tervalidasi.

## 14. Pengujian Minimum

- Unit test parser Claude dan Codex menggunakan fixture tersanitasi.
- Test deduplikasi event dan resume cursor.
- Test workspace normalization Windows dan platform lain yang digunakan.
- Test klasifikasi VS Code, CLI, dan unknown.
- Test sinkronisasi offline, retry, dan idempotency.
- Test redaction untuk path, credential, prompt, respons, dan source code.
- Test output ketika metrik token atau allowance tidak tersedia.
- Test migrasi schema SQLite.

## 15. Risiko dan Mitigasi

| Risiko | Dampak | Mitigasi |
|---|---|---|
| Format data lokal berubah | Collector berhenti membaca data | Adapter version-aware, fixture per versi, `tokenmon doctor` |
| Allowance tidak tersedia secara programatik | Persentase akun kosong | Tampilkan unavailable; local tracking tetap berjalan |
| Sesi yang sama terbaca ulang | Statistik terlalu besar | Cursor persisten dan event ID deterministik |
| Path membocorkan informasi proyek | Risiko privasi | Hash path dan gunakan alias tampilan |
| Tiga device offline bersamaan | Dashboard tidak mutakhir | Tampilkan `last_seen` dan freshness |
| Claude/Codex berjalan paralel | Event tertukar | Gunakan session ID, provider, device, dan timestamp |
| Provider mengubah kebijakan plan | Interpretasi allowance salah | Jangan turunkan allowance dari token; tampilkan sumber dan timestamp |

## 16. Tahapan Implementasi

### Fase 0 — Spike validasi

- Inventarisasi versi dan lokasi data lokal Claude Code serta Codex pada seluruh perangkat.
- Verifikasi field token, session ID, workspace, model, dan timestamp.
- Verifikasi apakah allowance dapat diambil tanpa scraping rapuh.
- Hasil fase ini adalah matriks sumber data dan keputusan go/no-go per metrik.

### Fase 1 — Collector satu perangkat

- SQLite lokal.
- Adapter Claude dan Codex.
- Agregasi per workspace dan sesi.
- Perintah `tokenmon`, `week`, dan `doctor`.

### Fase 2 — Sinkronisasi tiga perangkat

- Registrasi device.
- Endpoint sinkronisasi idempotent.
- Penyimpanan pusat.
- Perintah `devices`, `workspaces`, dan `sync`.

### Fase 3 — Allowance dan live monitoring

- Snapshot allowance jika sumber stabil tersedia.
- Freshness dan reset time.
- `tokenmon watch`.

## 17. Keputusan yang Masih Diperlukan

- Claude Max yang digunakan adalah tier 5x atau 20x.
- Sistem operasi pada perangkat kedua dan ketiga.
- Apakah semua perangkat dapat mengakses satu server internal atau memerlukan layanan internet.
- Pilihan penyimpanan pusat setelah kebutuhan jaringan diketahui.
- Lama retensi data lokal dan pusat.

## 18. Definisi Selesai

Tokenmon dianggap selesai untuk MVP ketika pengguna dapat memasang agent pada tiga perangkat, menjalankan satu perintah CLI dari salah satu perangkat, dan melihat penggunaan Claude/Codex per device dan workspace dengan data tersinkronisasi, tanpa mengumpulkan konten kerja dan tanpa menyamakan token lokal dengan allowance langganan.
