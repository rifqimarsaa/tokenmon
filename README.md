# Tokenmon

Tokenmon adalah internal CLI tool untuk memonitor penggunaan Claude Code dan Codex pada maksimal tiga perangkat. Tool ini membaca metadata session lokal secara read-only dan tidak memerlukan API key, login tambahan, atau dashboard web.

## Yang dimonitor

- Penggunaan per device, provider, workspace, dan session.
- Input, output, cache-read, dan cache-write token.
- Asal session: VS Code, CLI, atau `unknown`.
- Local share dalam bentuk progress bar.
- Allowance Codex dari snapshot resmi lokal jika tersedia.

Tokenmon tidak menyimpan prompt, respons, source code, command, credential, atau raw workspace path.

## Persyaratan

- Python 3.10 atau lebih baru.
- Claude Code dan/atau Codex sudah terpasang dan pernah digunakan.
- Windows PowerShell untuk mengikuti contoh berikut.

## Instalasi

### Menggunakan aplikasi desktop

Tokenmon didistribusikan sebagai **satu file**:

```text
dist\Tokenmon.exe
```

Salin file itu ke perangkat mana pun dan jalankan sekali. Setelah itu semuanya otomatis:

- **Mencari session sendiri.** Tokenmon memantau `~/.claude/projects` dan `~/.codex/sessions` (atau
  `CLAUDE_CONFIG_DIR` / `CODEX_HOME` bila di-set) dan hanya membaca baris baru, jadi angka berubah
  sekitar 2 detik setelah Claude Code atau Codex menulis. Tombol "Scan sekarang" memaksa cek saat itu juga.
- **Berjalan di latar belakang.** Menutup jendela menyembunyikannya ke ikon tray dekat jam. Klik ikon
  untuk membuka, klik kanan untuk "Mulai bersama Windows" atau "Keluar". Arahkan mouse ke ikon untuk
  melihat ringkasan token 5 jam dan persen kuota.
- **Mulai bersama Windows.** Pada pertama kali dijalankan, EXE mendaftarkan dirinya di
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (terbuka tersembunyi di tray). Matikan lewat
  menu tray. Hanya satu instance yang berjalan.
- **Menghubungkan Claude sendiri** (lihat di bawah).

Tab yang tersedia: Usage, Devices, Workspaces, dan Sessions. Python tidak diperlukan pada perangkat
yang menjalankan EXE. Claude Code dan/atau Codex tetap harus terpasang dan pernah digunakan.

### Membaca dashboard

Satu akun provider bisa dipakai di beberapa perangkat, tetapi provider hanya melaporkan persen untuk
**seluruh akun**. Karena itu dashboard memisahkan dua hal:

- **Dua gauge (Hari ini dan Minggu ini) = perangkat ini saja.** Untuk Claude, persennya adalah **kredit**
  yang dipakai komputer ini dibagi **kapasitas kredit mingguan paket Anda**. Limit Claude tidak dihitung
  dalam token mentah, melainkan dalam kredit: `(input + output + cache-read + cache-write) × tarif model`.
  Tarif per model, tier paket, dan kredit sesi 5 jam dibaca dari konfigurasi Claude Code sendiri
  (`~/.claude.json`; hanya tiga nilai itu yang dibaca, tidak ada yang disimpan). Kapasitas mingguan
  diperkirakan **5 × kredit sesi 5 jam** (Max 20x: 200.000 × 5 = 1.000.000; Max 5x: 250.000).
- **Seberapa pasti angkanya.** Anthropic tidak mempublikasikan kapasitas mingguan. Angka 5× dipilih karena
  paling cocok dengan data satu akun Max 20x: pada 10 bacaan persen mingguan, kredit perangkat ini ÷ 1,0M
  berbeda dari persen resmi paling banyak 2,6 poin, dan rumus kreditnya cocok dengan persen sesi 5 jam resmi
  (31,8% dihitung vs 33% resmi). Anggap gauge akurat sekitar ±5 poin dan bisa bergeser sampai ±25% bila
  kapasitas sebenarnya berbeda. Kredit dan kapasitas yang dipakai tertulis di sebelah judul kartu
  (`554.2K / 1.0M kredit`).
- **Bila paket tidak dikenal** (konfigurasi Claude Code tidak terbaca atau tier baru), gauge Claude memakai
  persen akun dan kartu menyebutnya.
- **Codex** tidak punya tabel kredit, jadi gauge Codex memakai persen resmi akun apa adanya.
- **Minggu ini:** kredit sejak awal jendela mingguan (hitung mundur "reset ..." mengikuti reset provider).
  Busur yang lebih terang di ujungnya adalah bagian yang terpakai hari ini. Hijau sampai 60%, kuning sampai
  85%, merah di atas itu.
- **Hari ini:** hari kalender lokal (sejak 00:00), bukan 24 jam. Bila jendela mingguan baru direset hari
  ini, hanya kredit sejak reset yang dihitung.
- **Nominal token** di bawah tiap gauge (ringkas dan angka penuh) adalah token dari **komputer ini**.
  Di sebelahnya `· akun 56%` adalah angka resmi akun sebagai pembanding.
- **Weekly · total akun** di bagian bawah kartu adalah angka **akun** yang resmi (semua perangkat dan
  produk): persen mingguan, bar, dan hitung mundur reset. Limit sesi 5 jam tidak ditampilkan.
- **Kenapa gauge bisa sama dengan angka akun:** bila minggu itu hampir semua pemakaian memang dari komputer
  ini, kedua angka praktis sama. Gauge turun di bawah angka akun ketika ada pemakaian dari tempat lain
  (claude.ai web, Cowork, atau perangkat lain), karena pemakaian itu tidak punya token di database ini.
- Persen Claude berupa bilangan bulat karena `/usage` memang membulatkannya. Codex melaporkan desimal.
- Animasi gauge mengikuti pengaturan Windows "Show animations". Bila dimatikan, nilai langsung berubah.

**Versi aplikasi** tampil di header ("v0.2.3"), footer, menu tray, dan properti file `Tokenmon.exe`
(Details → File version). Satu sumbernya adalah `__version__` di `src/tokenmon/__init__.py`.

### Kuota Claude (5 jam dan mingguan)

Berbeda dengan Codex, file session Claude Code **tidak** menyimpan persen kuota. Tokenmon memakai
tiga sumber, dari yang utama:

1. **`claude /usage` (utama, jalan juga di panel VS Code).** Tiap 2 menit Tokenmon menjalankan
   `claude -p "/usage"` di latar belakang (sekitar 1 detik, tanpa biaya token) dan membaca persen
   sesi 5 jam serta mingguan beserta waktu reset. Yang dipakai adalah `claude` di `PATH`, atau
   `claude.exe` bawaan ekstensi VS Code/Cursor. Tokenmon memakai login Claude Code sendiri dan tidak
   membaca credential apa pun. Keluaran ini masih berlabel *experimental* di dokumentasi Claude Code,
   jadi bila gagal, alasannya tampil di kartu Claude.
2. **Catatan limit tercapai** di file session, dibaca otomatis ("100% terpakai · reset ...").
3. **Status line** (cadangan, di bawah). Persen kuota yang berjalan juga bisa datang dari data
   `rate_limits` yang diberikan Claude Code ke status line. Saat start, Tokenmon memasangnya sendiri:

1. Membuat skrip kecil di `%LOCALAPPDATA%\tokenmon\claude-statusline.ps1` yang hanya menyimpan data
   kuota ke `claude-statusline.json` di folder yang sama.
2. Menambahkan `statusLine` ke `%USERPROFILE%\.claude\settings.json` setelah membuat backup
   `settings.json.tokenmon-backup` (backup yang sudah ada tidak ditimpa). Jika file itu sudah punya
   `statusLine` lain atau isinya rusak, Tokenmon tidak mengubah apa pun dan alasannya tampil di kartu.
3. Setelah Claude Code dibuka ulang, kartu menampilkan persen terpakai dan hitung mundur reset.

Catatan:
- Data kuota hanya diperbarui saat Claude Code menjalankan status line. Waktu pembaruan ditampilkan di kartu.
- Dokumentasi Claude Code tidak menyebut apakah status line berjalan di panel ekstensi VS Code.
  Jika kuota tetap kosong setelah mengirim pesan di panel VS Code, jalankan `claude` di terminal.
- "Jendela 5 jam ini" adalah jumlah token lokal sejak jendela 5 jam Claude dimulai. Itu bukan
  persen kuota.
- Setelah upgrade dari versi lama, scan pertama membaca ulang semua session sekali (sekitar 15 detik).
  Output token Claude akan naik sekitar 10% karena rekaman streaming kini dihitung lengkap.

### Build ulang EXE

Pada komputer development yang memiliki Python dan PyInstaller:

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

Hasilnya hanya `dist\Tokenmon.exe`. File kerja PyInstaller ditaruh di folder `%TEMP%`, jadi repo
tidak berisi `build\` atau `.spec`. Isi `dist` adalah build artifact dan tidak perlu masuk Git.

### Development: package Python dan CLI

Kode CLI tetap ada untuk development dan tidak dibuild menjadi EXE. Buka PowerShell di repository Tokenmon:

```powershell
git clone https://github.com/rifqimarsaa/tokenmon.git
cd tokenmon
python -m pip install -e .[gui] pyinstaller
tokenmon doctor
```

Jika `tokenmon` belum dikenali, buka terminal baru atau gunakan:

```powershell
python -m tokenmon doctor
```

## Penggunaan utama: satu command

Jalankan:

```powershell
tokenmon
```

Command ini otomatis melakukan scan incremental dan menampilkan satu dashboard berisi:

1. Account allowance dengan usage bar.
2. Penggunaan tujuh hari per device dan provider.
3. Local share bar setiap device/provider.
4. Delapan workspace dengan penggunaan terbesar.
5. Input, output, cache, dan total token.

Contoh tampilan:

```text
TOKENMON DASHBOARD

ACCOUNT ALLOWANCE
Claude Max   [??????????????????] unavailable
Codex        [################--] 91% remaining

LOCAL USAGE - LAST 7 DAY(S) BY DEVICE
Laptop       claude   453.8M  [###########-] 91.6%
Laptop       codex     41.8M  [#-----------]  8.4%

LOCAL USAGE - LAST 7 DAY(S) BY WORKSPACE
project-a    claude   191.3M  [#####-------] 38.6%
```

Bar allowance dan bar local share adalah dua metrik berbeda. Local share tidak menunjukkan sisa kuota langganan.

## Semua command

### Dashboard lengkap

```powershell
tokenmon
```

Scan dan tampilkan allowance, device/provider, serta workspace teratas dalam satu layar.

### Diagnostic

```powershell
tokenmon doctor
```

Memeriksa database, device ID, instalasi Claude/Codex, lokasi session, dan konfigurasi sync.

### Scan manual

```powershell
tokenmon scan
```

Mengimpor session yang baru atau berubah. Event yang sudah tersimpan tidak dihitung ulang.

### Hari ini

```powershell
tokenmon today
```

Menampilkan penggunaan 24 jam terakhir per device/provider.

### Tujuh hari

```powershell
tokenmon week
```

Menampilkan penggunaan tujuh hari terakhir per device/provider.

### Detail per device

```powershell
tokenmon devices
```

### Detail semua workspace

```powershell
tokenmon workspaces
```

Raw workspace path tidak ditampilkan.

### Detail session

```powershell
tokenmon sessions
```

Session ID ditampilkan dalam bentuk pendek bersama provider dan client.

### Live monitoring

```powershell
tokenmon watch
```

Tampilan diperbarui setiap 30 detik. Hentikan dengan `Ctrl+C`.

Gunakan interval lain:

```powershell
tokenmon watch --interval 10
```

### Mengganti nama device

```powershell
tokenmon device rename "Laptop Kantor"
```

### Sinkronisasi device

```powershell
tokenmon sync
```

Mengekspor statistik device aktif dan mengimpor statistik device lain dari folder `TOKENMON_SYNC_DIR`.

## Arti kolom

| Kolom | Arti |
|---|---|
| `Name` | Device, workspace, atau session. |
| `Provider/client` | Claude/Codex atau client VS Code/CLI. |
| `Input` | Input token dari metadata provider. |
| `Output` | Output token dari metadata provider. |
| `Cache` | Cache-read dan cache-write token. |
| `Total` | Total lokal yang dinormalisasi per provider. |
| `Local share` | Bagian penggunaan lokal dalam periode laporan. |

Pada Codex, cached input sudah termasuk dalam input sehingga cache tidak dijumlahkan dua kali ke total. Claude melaporkan input dan cache secara terpisah.

## Data lokal

Database default:

```text
%LOCALAPPDATA%\tokenmon\tokenmon.db
```

Biasanya:

```text
C:\Users\<username>\AppData\Local\tokenmon\tokenmon.db
```

Ubah lokasi dengan:

```powershell
$env:TOKENMON_HOME = "D:\PrivateData\tokenmon"
```

Mengganti lokasi berarti menggunakan database dan device ID berbeda.

## Sinkronisasi tiga perangkat

Satu perangkat berjalan sepenuhnya lokal dan tidak memerlukan sync.

Untuk beberapa perangkat, gunakan satu folder privat bersama seperti NAS, VPN drive, OneDrive privat, atau shared drive internal terenkripsi.

Konfigurasi sementara:

```powershell
$env:TOKENMON_SYNC_DIR = "D:\PrivateShare\tokenmon"
tokenmon sync
```

Konfigurasi permanen Windows:

```powershell
[Environment]::SetEnvironmentVariable(
    "TOKENMON_SYNC_DIR",
    "D:\PrivateShare\tokenmon",
    "User"
)
```

Buka terminal baru lalu jalankan pada setiap perangkat:

```powershell
tokenmon device rename "Nama Perangkat"
tokenmon scan
tokenmon sync
```

Setelah ketiga perangkat sync, jalankan lagi pada perangkat pertama:

```powershell
tokenmon sync
tokenmon
```

## File yang perlu dibagikan

### Untuk memasang aplikasi pada perangkat lain

Bagikan folder repository Tokenmon, minimal:

```text
tokenmon/
├── pyproject.toml
├── README.md
└── src/
    └── tokenmon/
```

Folder `tests/` boleh ikut dibagikan untuk validasi. `docs/PRD.md` boleh dibagikan sebagai dokumentasi, tetapi tidak dibutuhkan untuk menjalankan aplikasi.

Tidak perlu membagikan:

```text
.git/
.tokenmon*/
__pycache__/
*.egg-info/
```

### Untuk sinkronisasi statistik

Tokenmon otomatis membuat satu file per device di shared directory:

```text
tokenmon-<device-id>.json
```

Hanya file JSON tersebut yang perlu tersedia bagi perangkat lain. File berisi device, workspace alias/hash, session ID, model, timestamp, token, dan allowance snapshot. Jangan mengedit file secara manual.

## File yang tidak boleh dibagikan

Jangan bagikan:

```text
%USERPROFILE%\.claude\
%USERPROFILE%\.codex\
%LOCALAPPDATA%\tokenmon\tokenmon.db
credential atau token login Claude/OpenAI
session mentah Claude/Codex
```

Database dan file sync tidak memuat isi percakapan, tetapi tetap merupakan metadata privat. Jangan unggah ke repository publik.

## Test

Dari root repository:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v
```

Acceptance test manual:

```powershell
tokenmon doctor
tokenmon scan
tokenmon
tokenmon workspaces
tokenmon sessions
```

Yang perlu dipastikan:

- Database dan provider berstatus `OK`.
- Dashboard memiliki allowance dan local-share bars.
- Workspace tidak menampilkan full path.
- Session tidak menampilkan prompt atau respons.
- Scan ulang tidak menggandakan event lama.

## Troubleshooting

### Tidak ada data

Gunakan Claude Code atau Codex terlebih dahulu, lalu:

```powershell
tokenmon scan
tokenmon
```

### Scan ulang masih menemukan event

Normal jika Claude atau Codex sedang aktif karena session file terus berubah.

### Claude Max `unavailable`

Disengaja bila Claude Code belum mengirim data status line: Tokenmon tidak menebak kuota dari token lokal. Aplikasi desktop memasang status line sendiri saat start (lihat "Kuota Claude" di atas).

### Sync `NOT CONFIGURED`

Tetapkan `TOKENMON_SYNC_DIR`, buka terminal baru bila menggunakan konfigurasi permanen, lalu jalankan `tokenmon doctor`.

## Privasi dan batasan

- Tokenmon bukan billing system provider.
- Historical usage bergantung pada session metadata yang masih tersedia.
- Format session dapat berubah setelah update Claude/Codex.
- Allowance plan tidak memiliki hubungan linear dengan token lokal.
- Sinkronisasi MVP memakai folder bersama, bukan server multi-user.

## Uninstall

```powershell
python -m pip uninstall tokenmon-internal
```

Uninstall package tidak otomatis menghapus database. Hapus `%LOCALAPPDATA%\tokenmon\` secara manual hanya jika datanya sudah tidak dibutuhkan.
