# KAP Bot

KAP'taki **Özel Durum Açıklamalarını (ÖDA)** 7/24 izler. Yeni iş ilişkisi, sözleşme, anlaşma,
ihale, sipariş, iş birliği, varlık satışı, tahsilat gibi bildirimleri yakalar ve Telegram'a gönderir.

Örnek mesaj:

```
📢 GESAN · İhale Süreci / Sonucu
GİRİŞİM ELEKTRİK SANAYİ TAAHHÜT VE TİCARET A.Ş.

📝 İhale Sonucu hakkında
💰 497.777.777 TL
• İhale Konusu: H.889 Referanslı 154kV ... Yenileme (TTFO) İşi
• İhaleyi Açan Taraf: TEİAŞ Genel Müdürlüğü
• İhale Sonucu: İhale Şirketimiz Uhdesinde Kalmıştır
• İhale Bedeli: 497.777.777 TL

Şirketimizin; TEİAŞ Genel Müdürlüğü tarafından gerçekleştirilen ...

🕒 09.10.2026 11:33 · KAP'ta aç
```

## Nasıl çalışır?

1. KAP'tan ÖDA listesini çeker (KAP sitesinin kendi kullandığı servis): günlük özet modunda her
   sabah özet saatinde, anlık modda 30 saniyede bir.
2. Daha önce görmediği her bildirimi filtreden geçirir (`ayarlar.toml` → `[filtre]`):
   - **Konu başlığı** "Yeni İş İlişkisi", "İhale Süreci / Sonucu", "Finansal Duran Varlık Satışı"
     gibi bir başlıksa direkt gönderir.
   - "Özel Durum Açıklaması (Genel)" gibi başlıklarda **özette** "sözleşme", "anlaşma",
     "sipariş", "iş birliği", "ihale" gibi bir kelime geçiyorsa gönderir.
   - Hisse geri alımı, borçlanma aracı, genel kurul, kredi notu gibi gürültüleri eler.
3. Yakaladığı bildirimin detay sayfasını okur. Tutarı, karşı tarafı ve açıklama metnini mesaja ekler.
4. Gördüğü her bildirimi `veri/kap.db` dosyasına yazar. Aynı haber iki kez gelmez, bot yeniden
   başlasa da kaldığı yerden devam eder.

Son 7 günle yapılan denemede 801 ÖDA bildiriminden 38'i yakalandı.

---

## 1. Telegram botunu oluştur (2 dk)

1. Örnek ayar dosyasını kopyala (gerçek token'lar bu kopyada durur, `.gitignore` sayesinde GitHub'a gitmez):
   ```
   cp ayarlar.example.toml ayarlar.toml
   ```
   Windows'ta: `copy ayarlar.example.toml ayarlar.toml`
2. Telegram'da **@BotFather**'ı aç → `/newbot` yaz → bota bir isim ver.
3. Verdiği token'ı (`123456789:AAH...`) `ayarlar.toml` içindeki `bot_token`'a yapıştır.
4. Telegram'da kendi botunu bul ve ona `/start` yaz.
5. Chat ID'ni bul:
   ```
   python bot.py --chat-id
   ```
   Çıkan değeri `ayarlar.toml` içindeki `chat_id`'ye yaz.

> Bir **kanala** göndermek istersen botu kanala yönetici olarak ekle, kanala bir mesaj at,
> sonra `--chat-id` komutunu çalıştır (kanal ID'leri `-100...` ile başlar).

## 2. Önce bilgisayarında dene (isteğe bağlı)

```
pip install -r requirements.txt
python bot.py --tara 7          # son 7 günde nelerin yakalanacağını listeler, göndermez
python bot.py --onizle 1681807  # bir bildirimin mesajını ekrana basar
python bot.py --ozet            # son 24 saatin özetini Telegram'a gönderir
python bot.py --test            # Telegram'a tek bir bildirimle deneme mesajı gönderir
```

## 3. Sunucuya kur (Ubuntu/Debian)

Klasörü sunucuya kopyala (Windows'ta PowerShell'den):

```
scp -r ".\Kap Bot" kullanici@SUNUCU_IP:~/kap-bot
```

Sunucuda:

```
cd ~/kap-bot
sudo bash kurulum/kur.sh
```

Betik şunları yapar: Python sanal ortamını kurar, bağımlılıkları yükler, KAP bağlantısını dener
ve botu `kap-bot` adlı systemd servisi olarak başlatır. Bot sunucu yeniden başladığında kendiliğinden
açılır, çökerse 10 saniye içinde yeniden kalkar.

> **Günlük özet modunda** ilk özet bir sonraki 09:00'da gelir ve önceki 09:00'dan beri
> yayımlananları kapsar. Çalıştığını hemen görmek için: `.venv/bin/python bot.py --ozet`

## Yönetim komutları

| Ne yapmak istiyorsun | Komut |
|---|---|
| Canlı logları izle | `journalctl -u kap-bot -f` |
| Durumunu gör | `systemctl status kap-bot` |
| Ayar değiştirdikten sonra yeniden başlat | `sudo systemctl restart kap-bot` |
| Durdur / başlat | `sudo systemctl stop kap-bot` / `sudo systemctl start kap-bot` |
| Kaldır | `sudo systemctl disable --now kap-bot && sudo rm /etc/systemd/system/kap-bot.service` |

## Gönderim şekli: günlük özet ya da anlık

`ayarlar.toml` → `[telegram]` içindeki `gonderim` ayarı:

- **`gonderim = "gunluk"`** (şu anki ayar): Her gün `ozet_saati`'nde (`"09:00"`) tek bir özet
  mesajı gelir. Mesaj, bir önceki özetten bu yana yayımlanan uygun bildirimleri sıralar: şirket,
  konu, saat, tutar ve KAP linki. Özet çok uzunsa birkaç mesaja bölünür. O gün bildirim yoksa
  "Kriterlere uyan bildirim yok" yazar. Böylece botun çalıştığını da görmüş olursun.
  - Birden fazla özet saati verebilirsin: `ozet_saati = ["09:00", "18:30"]`.
  - Döviz tutarlarının yanında TCMB döviz alış kuruyla TL karşılığı yazar
    (`40 milyon EUR (≈ 2,21 milyar TL)`). Bildirim TL karşılığını zaten veriyorsa ayrıca
    çevirmez. Kullanılan kur, özetin başında yazar.
  - `ozette_kisa_konular` listesindeki başlıklar (pay alım-satım, kâr payı, kayıtlı sermaye
    tavanı) özetin sonunda "Diğer bildirimler" altında tek satır olarak listelenir.
  - Bot özet saatinde kapalıysa, açıldığında kaçırdığı özeti hemen gönderir (en fazla 3 gün geriye).
  - Özetin neye benzediğini görmek için: `python bot.py --ozet` (son 24 saati hemen gönderir).
- **`gonderim = "anlik"`**: Her bildirim KAP'ta yayımlandığı anda (30 sn içinde) ayrı mesaj olarak
  gelir. Bu modda `sessiz_saatler = "23:00-08:00"` ayarı, gece gelen mesajların telefonu
  öttürmemesini sağlar.

Saatler Türkiye saatidir, sunucunun saat diliminden etkilenmez.

## Filtreyi ayarlamak

Her şey `ayarlar.toml` içinde, kod değiştirmen gerekmez:

- `her_zaman_gonder`: Bu konu başlıkları her zaman gelir. Şirketin satın almalarını da (para
  çıkışı) görmek istersen "Finansal Duran Varlık Edinimi" satırının başındaki `#` işaretini kaldır.
- `anahtar_kelimeler`: Özette bunlardan biri geçerse gelir. Kelimenin başını yazmak yeterli.
- `haric_kelimeler`: Bunlar geçerse gelmez (ör. "esas sözleşme" tadilleri).
- `sadece_hisseler`: Sadece belli hisseleri takip etmek için, ör. `["ASELS", "THYAO"]`.

Değişiklikten sonra `python bot.py --tara 7` ile sonucu kontrol et, sonra servisi yeniden başlat.

## Dosyalar

| Dosya | Ne işe yarar |
|---|---|
| `bot.py` | Ana program: döngü, veritabanı, komut satırı |
| `kap.py` | KAP'tan liste ve bildirim detayını çeker |
| `filtre.py` | Hangi bildirimin gönderileceğine karar verir, tutarları bulur |
| `telegram_gonderici.py` | Mesajı hazırlar ve Telegram'a gönderir |
| `kur.py` | TCMB döviz kurlarını çeker (TL karşılıkları için) |
| `ayarlar.example.toml` | Örnek ayar dosyası (token'sız); `ayarlar.toml` adıyla kopyalanır |
| `ayarlar.toml` | Senin token, chat ID ve filtre ayarların (GitHub'a gitmez) |
| `kurulum/` | Linux kurulum betiği ve systemd servis dosyası |

## Notlar

- KAP'ın resmi, herkese açık bir API'si yok. Bot, kap.org.tr sitesinin kendi kullandığı servisi
  kullanıyor. KAP sitesini değiştirirse botun da güncellenmesi gerekebilir. Loglarda
  "KAP kontrolü başarısız" görürsen sebep büyük ihtimalle budur.
- Kontrol aralığını 15 saniyenin altına indirme. KAP'ı gereksiz yormamak ve engellenmemek için.
- Token'ı dosyada tutmak istemezsen `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` ortam
  değişkenleri dosyadaki değerleri ezer.
- Bu bot yatırım tavsiyesi vermez, sadece KAP bildirimlerini iletir.
