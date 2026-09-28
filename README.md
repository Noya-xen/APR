# Meteora APR Telegram Bot

link project: https://github.com/Noya-xen/APR

Bot Telegram read-only untuk menerima mint token Solana lalu menampilkan pool Meteora DLMM yang ditemukan dalam format HTML Telegram dengan emoji, APR fee 24 jam, farm APR, TVL, market cap token, fee pool, bin step, serta volume 15 menit, 1 jam, dan 24 jam.

## Fitur

- Input mint token Solana langsung lewat Telegram.
- Menerima alamat pool Meteora secara langsung.
- Mencari pool pada sisi `token_x` dan `token_y`.
- Menampilkan market cap token yang dicari.
- Menampilkan volume 15 menit, 1 jam, dan 24 jam.
- Menampilkan fee dasar dan bin step setiap pool.
- Menggunakan format HTML Telegram agar output lebih rapi.
- Menyaring pool dengan TVL sangat kecil agar APR tidak menyesatkan.
- Menyediakan alert berkala untuk pool tertentu dengan interval default 15 menit.
- Tidak meminta private key dan tidak mengirim transaksi.
- Mengambil data dari Meteora DLMM Data API resmi.

## Menjalankan di Windows

1. Install Python 3.10 atau lebih baru.
2. Buat bot lewat `@BotFather` dan salin tokennya.
3. Salin `.env.example` menjadi `.env`, lalu isi `TELEGRAM_BOT_TOKEN`.
4. Jalankan:

```powershell
python bot.py
```

## Perintah Telegram

- `/start` atau `/help`
- `/alerts` untuk melihat alert aktif.
- `/stopalerts` untuk mematikan semua alert pada chat.
- Kirim mint token, misalnya `So11111111111111111111111111111111111111112`
- `/apr <mint>`

Setelah hasil pool muncul, tekan `🔔 Set Alert`, pilih pool, lalu pilih interval 5, 15, 30, 60 menit, atau masukkan interval custom 1–1440 menit. Konfigurasi alert tersimpan di `alerts.json` dan file tersebut tidak di-upload ke GitHub.

## Catatan APR

Meteora mengembalikan `apr` sebagai APR fee 24 jam dalam bentuk desimal, misalnya `0.3344` berarti `33.44%`. Bot juga menampilkan `farm_apr` dan estimasi total `apr + farm_apr`. API Meteora menyediakan candle volume 5 menit, sehingga volume 15 menit dihitung dari tiga candle 5 menit terakhir. Secara default, pool dengan TVL di bawah `$1,000` disembunyikan; ubah `MIN_POOL_TVL_USD` di `.env` bila diperlukan. APR dan volume bersifat berubah-ubah dan tidak memperhitungkan impermanent loss.

Sumber API: https://dlmm.datapi.meteora.ag/swagger-ui/

## Keamanan

- Jangan commit `.env`.
- Untuk bot pribadi, isi `ALLOWED_USER_IDS` dengan Telegram ID yang diizinkan.
- Bot ini hanya membaca data publik; jangan menambahkan private key ke project ini.

## Disclaimer

Data APR bukan jaminan profit. Gunakan untuk riset dan lakukan verifikasi sendiri sebelum menyediakan likuiditas.

> Built by: Noya-xen | [@xinomixo](https://x.com/XinoMixo)
