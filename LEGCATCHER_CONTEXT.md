# LegCatcher Strateji Geliştirme — Devam Bağlamı

## Hedef
3 dakikalık mumlarla (BTCUSDT Binance Futures) günlük 6-7 trend leg'ini yakalayan bir TradingView stratejisi. Her leg 1.5-3 saat sürüyor. Günde 4-5 trade hedefi.

## Aktif Strateji Dosyası
`strategies/LegCatcher_V3_TV.pine` — V3.1 (dosya adı V3 ama içerik V3.1)

## V3.1 Stratejinin Mantığı
1. **Pivot High/Low tespiti** — `ta.pivothigh` / `ta.pivotlow` ile swing noktaları bul
2. **Market Structure** — HH+HL = uptrend, LH+LL = downtrend
3. **Structure break** — Trend değişimi tespit edilince giriş (long veya short)
4. **ADX filtresi** — ADX < threshold → choppy piyasa → trade yok
5. **Choppiness Index** — CI > threshold → trade yok
6. **Stop loss** — Pivot-based (son pivot altı/üstü) veya ATR-based
7. **Trailing stop** — Pivot trail (yeni pivot oluşunca SL güncellenir) + ATR trail
8. **Risk** — Günlük max kayıp %2, max 6 trade/gün, kayıp sonrası 10 bar cooldown

## Versiyon Geçmişi

| Versiyon | Yaklaşım | Sonuç |
|---|---|---|
| V1 | Momentum chase (2/3 aynı yönde mum → gir) | Zarar — tepeyi alıp dibi satıyor |
| V2 | Exhaustion reversal (yorgunluk + güçlü ters mum) | Zarar — çok gürültüye duyarlı |
| V3 | Market structure (pivot, HH/HL/LH/LL) | 15dk'da -698 (trendi yakaladı ama chop'ta kanıyor) |
| V3.1 | V3 + ADX + Choppiness Index chop filtresi | **15dk'da +2,326** |

## 3dk Chart Parametre Optimizasyonu (Devam Ediyor)

| Pivot | ADX | Chop | Sonuç | Not |
|---|---|---|---|---|
| 7/7 | 20 | 55 | -505 | Çok gürültülü, sahte pivot |
| 20/20 | 25 | 50 | +360 | Karlı ama 11 trade, çok pasif |
| 15/15 | 20 | 55 | +503 | 14 trade/10gün, hala az |
| **10/10** | **18** | **58** | **?** | **Sıradaki test** |

## Önemli Dersler
- Karmaşıklık ≠ karlılık (Swinginess projesi 31 metrikle karlılık bulamadı, basit pivot+ADX çalıştı)
- 15dk chart'ta V3.1 zaten karlı (+2,326), 3dk'da parametre ayarı gerekiyor
- Pivot değeri timeframe'e göre ölçeklenmeli (15dk'da 7/7 = 105dk onay, 3dk'da 7/7 = 21dk onay)
- Chop filtresi (ADX + Choppiness Index) en büyük farkı yarattı

## Sıradaki Adımlar
1. Pivot 10/10, ADX 18, Chop 58 testi (3dk chart)
2. Farklı coinlerde test (ETHUSDT, SOLUSDT)
3. Parametre hassasiyeti testi (±2 değişiklik sonuçları ne kadar etkiliyor)
4. Karlı parametre bulununca → Python Scalper Bot'a entegre et
