"""Generate E-Scenario Strategy Report PDF."""
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm

desktop = os.path.expanduser("~") + "/Desktop"
path = os.path.join(desktop, "E_Scenario_Strategy_Report.pdf")

doc = SimpleDocTemplate(path, pagesize=A4, topMargin=20*mm, bottomMargin=15*mm, leftMargin=15*mm, rightMargin=15*mm)
styles = getSampleStyleSheet()

title_style = ParagraphStyle("T", parent=styles["Title"], fontSize=20, spaceAfter=10, textColor=colors.HexColor("#1a73e8"))
h1 = ParagraphStyle("H1a", parent=styles["Heading1"], fontSize=14, spaceAfter=6, textColor=colors.HexColor("#1a73e8"))
body = ParagraphStyle("B", parent=styles["Normal"], fontSize=9, spaceAfter=4)
small = ParagraphStyle("S", parent=styles["Normal"], fontSize=8, spaceAfter=2, textColor=colors.HexColor("#666666"))

def mt(data, cw=None):
    t = Table(data, colWidths=cw, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1a73e8")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTSIZE", (0,0), (-1,0), 9), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,1), (-1,-1), 8), ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f5f8ff")]),
        ("TOPPADDING", (0,0), (-1,-1), 3), ("BOTTOMPADDING", (0,0), (-1,-1), 3),
    ]))
    return t

story = []

# TITLE
story.append(Paragraph("ETHUSDT E-Scenario Strategy Report", title_style))
story.append(Paragraph("Adaptive PMax + Keltner Channel + Graduated DCA [1, 1.5, 2, 2.5] + Graduated TP [50, 60, 70, 80]", body))
story.append(Paragraph("Backtest: 360 gun (17 Mart 2025 - 17 Mart 2026) | Engine: Rust (PyO3) | Tarih: 2026-03-17", small))
story.append(Spacer(1, 8*mm))

# PERFORMANS
story.append(Paragraph("Performans Ozeti (118 gun compound)", h1))
story.append(mt([
    ["Metrik", "Deger"],
    ["Sembol", "ETHUSDT Perpetual Futures"],
    ["Timeframe", "3 dakika (3m)"],
    ["Baslangic Kasa", "$10,000"],
    ["Final Kasa (118 gun)", "$1,335,820"],
    ["Net Getiri", "+13,258.2%"],
    ["Max Drawdown", "30.4%"],
    ["Win Rate", "84.4%"],
    ["Profit Factor", "1.70"],
    ["Sharpe Ratio", "0.103"],
    ["Toplam Trade", "5,751"],
    ["Leverage", "25x"],
], [120, 200]))
story.append(Spacer(1, 6*mm))

# PMAX
story.append(Paragraph("ADIM 1: Adaptive PMax Parametreleri", h1))
story.append(Paragraph("PMax giris/cikis sinyali uretir. Adaptive modu her 29 barda parametreleri gunceller.", body))
story.append(mt([
    ["Parametre", "Deger", "Aciklama"],
    ["vol_lookback", "580", "Volatilite median penceresi (bar)"],
    ["flip_window", "440", "Yon degisim sayma penceresi (bar)"],
    ["mult_base", "4.0", "ATR carpan tabani"],
    ["mult_scale", "1.25", "Volatiliteye bagli carpan olcekleme"],
    ["ma_base", "3", "MA uzunluk tabani"],
    ["ma_scale", "5.5", "Trende bagli MA olcekleme"],
    ["atr_base", "19", "ATR period tabani"],
    ["atr_scale", "1.5", "Flip sayisina bagli ATR olcekleme"],
    ["update_interval", "29", "Parametre guncelleme sikligi (bar)"],
], [90, 50, 220]))
story.append(Paragraph("Sabit base: atr_period=10, atr_multiplier=3.0, ma_length=10, ma_type=EMA, source=hl2", small))
story.append(Spacer(1, 6*mm))

# KELTNER
story.append(Paragraph("ADIM 2: Keltner Channel", h1))
story.append(mt([
    ["Parametre", "Deger", "Aciklama"],
    ["kc_length", "5", "KC EMA periyodu"],
    ["kc_multiplier", "0.8", "KC ATR carpani"],
    ["kc_atr_period", "3", "KC ATR periyodu"],
], [90, 50, 220]))
story.append(Spacer(1, 6*mm))

# GRADUATED DCA
story.append(Paragraph("ADIM 3: Kademeli DCA", h1))
story.append(Paragraph("Her DCA adiminda artan margin carpani. Derin DCA = daha agresif entry duzeltme.", body))
story.append(mt([
    ["DCA Adimi", "Carpan", "Ornek ($10K, %3 tier)"],
    ["Giris", "1.0x (base)", "$300 margin x 25 = $7,500"],
    ["DCA 1", "1.0x", "$300 x 25 = $7,500"],
    ["DCA 2", "1.5x", "$450 x 25 = $11,250"],
    ["DCA 3", "2.0x", "$600 x 25 = $15,000"],
    ["DCA 4", "2.5x", "$750 x 25 = $18,750"],
    ["TOPLAM", "8.0x", "$2,400 margin = $60,000 notional"],
], [70, 70, 220]))
story.append(Spacer(1, 6*mm))

# GRADUATED TP
story.append(Paragraph("ADIM 4: Kademeli TP", h1))
story.append(Paragraph("DCA derinligine gore TP orani artar. Buyuk pozisyon = agresif kar alma = DD kontrolu.", body))
story.append(mt([
    ["DCA Derinligi", "TP Orani", "Mantik"],
    ["dca_fills = 1", "%50", "Hafif pozisyon, trend devam edebilir"],
    ["dca_fills = 2", "%60", "Orta, standart kar alma"],
    ["dca_fills = 3", "%70", "Buyuk pozisyon, riski azalt"],
    ["dca_fills = 4", "%80", "Max pozisyon, hizla kucult"],
], [85, 70, 205]))
story.append(Spacer(1, 6*mm))

# DYNAMIC COMP
story.append(Paragraph("ADIM 5: Dynamic Compounding", h1))
story.append(mt([
    ["Kasa Araligi", "Margin %", "Ornek"],
    ["< $30,000", "%3.0", "$10K x 3% = $300"],
    ["$30K - $150K", "%5.0", "$50K x 5% = $2,500"],
    ["> $150,000", "%2.25", "$200K x 2.25% = $4,500"],
], [100, 60, 200]))
story.append(Spacer(1, 6*mm))

# FILTRELER
story.append(Paragraph("Filtreler ve Risk Yonetimi", h1))
story.append(mt([
    ["Filtre/Risk", "Parametre", "Mantik"],
    ["EMA Trend", "EMA(144)", "LONG: fiyat > EMA, SHORT: fiyat < EMA"],
    ["RSI", "RSI(28), OB=65, OS=35", "LONG: RSI < 65, SHORT: RSI > 35"],
    ["ATR Volatilite", "ATR(50), %20 pctl", "Dusuk volatilite = islem yapma"],
    ["PCT Hard Stop", "%2.0", "DCA full sonrasi %2.0 kayip = kapat"],
    ["Max DCA Steps", "4", "Maksimum 4 DCA adimi"],
], [80, 100, 180]))

story.append(PageBreak())

# HAFTALIK
story.append(Paragraph("Haftalik Sonuclar (53 Hafta, Her Hafta $10K)", h1))

wdata = [["W#", "Donem", "Final", "PnL%", "DD%", "WR%", "Tr", "PF"]]
weeks = [
    (1,"03/17-03/24","$17,450","+74.5%","8.3%","84.8%","394","3.35"),
    (2,"03/24-03/31","$15,404","+54.0%","14.9%","81.7%","405","2.30"),
    (3,"03/31-04/07","$12,528","+25.3%","13.7%","82.5%","343","1.65"),
    (4,"04/07-04/14","$16,392","+63.9%","14.7%","81.4%","263","1.97"),
    (5,"04/14-04/21","$13,082","+30.8%","16.7%","89.6%","318","2.20"),
    (6,"04/21-04/28","$12,300","+23.0%","13.6%","83.4%","302","2.03"),
    (7,"04/28-05/05","$14,139","+41.4%","8.5%","82.4%","392","2.47"),
    (8,"05/05-05/12","$12,577","+25.8%","18.7%","80.8%","271","1.61"),
    (9,"05/12-05/19","$14,560","+45.6%","19.7%","85.4%","335","1.66"),
    (10,"05/19-05/26","$12,868","+28.7%","23.7%","83.6%","292","1.70"),
    (11,"05/26-06/02","$10,490","+4.9%","18.0%","79.7%","212","1.44"),
    (12,"06/02-06/09","$12,751","+27.5%","10.1%","82.3%","368","1.99"),
    (13,"06/09-06/16","$12,930","+29.3%","20.6%","83.5%","322","1.74"),
    (14,"06/16-06/23","$18,813","+88.1%","12.3%","88.8%","314","3.56"),
    (15,"06/23-06/30","$16,724","+67.2%","15.2%","87.9%","404","2.68"),
    (16,"06/30-07/07","$13,338","+33.4%","5.6%","82.3%","373","2.31"),
    (17,"07/07-07/14","$15,557","+55.6%","10.7%","86.4%","382","3.08"),
    (18,"07/14-07/21","$12,503","+25.0%","19.6%","82.8%","372","1.57"),
    (19,"07/21-07/28","$14,932","+49.3%","17.2%","84.6%","397","2.11"),
    (20,"07/28-08/04","$13,698","+37.0%","14.3%","84.5%","296","1.97"),
    (21,"08/04-08/11","$13,920","+39.2%","9.5%","80.0%","315","2.16"),
    (22,"08/11-08/18","$13,069","+30.7%","7.9%","80.9%","257","2.02"),
    (23,"08/18-08/25","$16,044","+60.4%","10.7%","82.2%","325","2.36"),
    (24,"08/25-09/01","$21,220","+112.2%","10.4%","89.2%","379","3.26"),
    (25,"09/01-09/08","$13,059","+30.6%","6.6%","88.1%","327","2.65"),
    (26,"09/08-09/15","$10,367","+3.7%","18.0%","76.6%","401","1.54"),
    (27,"09/15-09/22","$8,628","-13.7%","21.9%","83.3%","335","1.18"),
    (28,"09/22-09/29","$15,617","+56.2%","7.3%","86.2%","399","2.91"),
    (29,"09/29-10/06","$10,565","+5.7%","13.4%","82.7%","382","1.61"),
    (30,"10/06-10/13","$22,552","+125.5%","8.9%","86.4%","323","4.07"),
    (31,"10/13-10/20","$13,791","+37.9%","15.5%","84.4%","315","1.80"),
    (32,"10/20-10/27","$7,199","-28.0%","30.4%","77.5%","173","0.64"),
    (33,"10/27-11/03","$11,410","+14.1%","16.2%","82.6%","276","1.64"),
    (34,"11/03-11/10","$14,136","+41.4%","20.1%","83.6%","311","1.70"),
    (35,"11/10-11/17","$26,550","+165.5%","14.5%","85.3%","355","3.09"),
    (36,"11/17-11/24","$14,377","+43.8%","16.9%","79.1%","273","1.76"),
    (37,"11/24-12/01","$12,520","+25.2%","15.9%","84.4%","346","1.86"),
    (38,"12/01-12/08","$15,455","+54.5%","14.9%","85.4%","342","2.30"),
    (39,"12/08-12/15","$19,607","+96.1%","11.3%","93.1%","332","4.39"),
    (40,"12/15-12/22","$13,679","+36.8%","11.9%","86.1%","237","2.16"),
    (41,"12/22-12/29","$8,339","-16.6%","24.0%","81.4%","307","1.03"),
    (42,"12/29-01/05","$11,525","+15.2%","9.5%","84.5%","374","2.49"),
    (43,"01/05-01/12","$13,924","+39.2%","9.5%","81.4%","393","3.37"),
    (44,"01/12-01/19","$12,928","+29.3%","6.2%","86.7%","392","2.64"),
    (45,"01/19-01/26","$11,297","+13.0%","18.2%","88.6%","272","1.85"),
    (46,"01/26-02/02","$16,624","+66.2%","10.7%","87.5%","313","2.86"),
    (47,"02/02-02/09","$8,042","-19.6%","24.1%","75.0%","64","0.69"),
    (48,"02/09-02/16","$14,156","+41.6%","11.2%","87.2%","282","1.81"),
    (49,"02/16-02/23","$11,953","+19.5%","15.8%","82.6%","368","1.81"),
    (50,"02/23-03/02","$14,850","+48.5%","16.6%","82.9%","310","1.72"),
    (51,"03/02-03/09","$10,480","+4.8%","20.4%","80.2%","308","1.30"),
    (52,"03/09-03/16","$9,527","-4.7%","18.4%","80.3%","279","1.23"),
    (53,"03/16-03/17","$10,136","+1.4%","3.2%","68.8%","16","1.58"),
]
losing = {27, 32, 41, 47, 52}
for w in weeks:
    wdata.append([str(w[0])] + list(w[1:]))

t = Table(wdata, colWidths=[22, 62, 52, 42, 32, 32, 30, 32], repeatRows=1)
scmds = [
    ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1a73e8")),
    ("TEXTCOLOR", (0,0), (-1,0), colors.white),
    ("FONTSIZE", (0,0), (-1,0), 7), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE", (0,1), (-1,-1), 6.5), ("ALIGN", (0,0), (-1,-1), "CENTER"),
    ("GRID", (0,0), (-1,-1), 0.4, colors.HexColor("#dddddd")),
    ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f5f8ff")]),
    ("TOPPADDING", (0,0), (-1,-1), 2), ("BOTTOMPADDING", (0,0), (-1,-1), 2),
]
for i, w in enumerate(weeks):
    if w[0] in losing:
        ri = i + 1
        scmds.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor("#fff0f0")))
        scmds.append(("TEXTCOLOR", (3, ri), (3, ri), colors.red))
t.setStyle(TableStyle(scmds))
story.append(t)
story.append(Spacer(1, 3*mm))
story.append(Paragraph("Karli: 48/53 (%90.6) | Zararli: 5/53 | Ort. haftalik: +37.8% | Max DD: 30.4%", body))

story.append(PageBreak())

# AYLIK
story.append(Paragraph("Aylik Sonuclar (12 Ay, Her Ay $10K)", h1))
story.append(mt([
    ["Ay", "Final", "PnL%", "DD%", "WR%", "Trades", "PF"],
    ["Mart 2025", "$48,688", "+386.9%", "29.4%", "81.3%", "1,478", "1.73"],
    ["Nisan 2025", "$149,454", "+1,394.5%", "15.1%", "84.1%", "1,478", "2.58"],
    ["Mayis 2025", "$23,964", "+139.6%", "22.9%", "83.5%", "1,344", "1.57"],
    ["Haziran 2025", "$39,954", "+299.5%", "21.4%", "83.9%", "1,430", "2.08"],
    ["Temmuz 2025", "$36,997", "+270.0%", "25.8%", "86.2%", "1,585", "1.85"],
    ["Agustos 2025", "$77,418", "+674.2%", "19.2%", "82.3%", "1,471", "2.12"],
    ["Eylul 2025", "$23,659", "+136.6%", "20.5%", "84.7%", "1,671", "1.95"],
    ["Ekim 2025", "$33,737", "+237.4%", "33.6%", "83.3%", "1,399", "1.65"],
    ["Kasim 2025", "$197,608", "+1,876.1%", "31.1%", "85.6%", "1,627", "2.32"],
    ["Aralik 2025", "$64,335", "+543.4%", "34.4%", "86.5%", "1,465", "1.95"],
    ["Ocak 2026", "$27,840", "+178.4%", "42.7%", "85.0%", "1,651", "1.77"],
    ["Subat 2026", "$29,785", "+197.8%", "33.4%", "84.4%", "1,214", "1.57"],
], [70, 60, 60, 40, 40, 45, 35]))
story.append(Paragraph("12 ayin 12si karli. Ort. aylik: +445%", body))
story.append(Spacer(1, 6*mm))

# GUNLUK
story.append(Paragraph("360 Gun Gunluk Ozet", h1))
story.append(mt([
    ["Metrik", "Deger"],
    ["Aktif Gun", "260 / 365"],
    ["Karli Gun", "199 (%76.5)"],
    ["Zararli Gun", "61"],
    ["Islemsiz Gun", "14"],
    ["Max Ust Uste Kayip", "4 gun"],
    ["Max Ust Uste Kazanc", "14 gun"],
    ["Max DD (tek gun)", "%21.0"],
    ["DD > %20 Gun", "1"],
    ["DD > %30 Gun", "0"],
], [130, 130]))
story.append(Spacer(1, 6*mm))

# EVRIM
story.append(Paragraph("Strateji Evrimi (118 gun)", h1))
ev = Table([
    ["Versiyon", "Final", "PnL%", "DD%", "WR%", "PF"],
    ["Mevcut: DCA[1,1,1,1] TP=50%", "$341K", "+3,312%", "34.2%", "80.8%", "1.76"],
    ["Kad.DCA: [1,1.5,2,2.5] TP=50%", "$1.13M", "+11,152%", "37.0%", "82.5%", "1.66"],
    ["E: DCA+TP [50,60,70,80]", "$1.34M", "+13,258%", "30.4%", "84.4%", "1.70"],
], colWidths=[150, 50, 55, 38, 38, 35])
ev.setStyle(TableStyle([
    ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1a73e8")),
    ("TEXTCOLOR", (0,0), (-1,0), colors.white),
    ("FONTSIZE", (0,0), (-1,0), 8), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE", (0,1), (-1,-1), 8), ("ALIGN", (0,0), (-1,-1), "CENTER"),
    ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#cccccc")),
    ("BACKGROUND", (0,3), (-1,3), colors.HexColor("#e8f5e9")),
    ("FONTNAME", (0,3), (-1,3), "Helvetica-Bold"),
]))
story.append(ev)
story.append(Spacer(1, 6*mm))

# UYARI
story.append(Paragraph("Uyari", h1))
story.append(Paragraph("Bu backtest sonuclari gecmis veriye dayalidir. Gercek piyasada slippage, funding rate, likidite farklari ve exchange gecikmeleri nedeniyle sonuclar farkli olabilir.", body))
story.append(Paragraph("Planlanan iyilestirmeler: DCA Hiz Siniri (min 10 bar) + Reversal Cooldown (min 15 bar).", body))

doc.build(story)
print(f"PDF: {path}")
