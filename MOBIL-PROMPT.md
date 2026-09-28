# Prompt för mobilversion av Filmrulle (klistra in i ny chatt)

Jag vill bygga en mobilversion av mitt befintliga desktopprogram **Filmrulle** —
en fristående fotoredigerare för analoga/retro filmsimuleringar. Skapa den i
`C:\Claude\filmrulle-mobil`.

## Utgångspunkt: desktop-versionen

Källkoden finns i `C:\Claude\filmrulle\filmrulle.py` (enfilsapp, Python/tkinter
/numpy/Pillow, ~3800 rader). **Läs den innan du börjar** — särskilt:

- **`process(arr, grade)`** — hela bildpipelinen i float32 (0–1), stegvis:
  exponering → vitbalans (temp/tint) → kontrast → per-kanal-kurvor →
  användarens tonkurva (PCHIP) → mättnad → split-toning (shadow/highlight_tint)
  → ev. svartvitt → fade (matt-lyft) → clarity → skärpa → halation → korn →
  vinjett. Detta är appens hjärta och måste ge **samma visuella resultat** på
  mobilen.
- **`FILMS`-listan** — 26 filmrecept där varje film bara är en uppsättning
  parametrar (`Grade`-dataclass) till samma pipeline. Recepten ska
  transplanteras rakt av, inte återuppfinnas.
- **`Grade`** — alla parametrar och deras skalor (temp/tint −100..100,
  grain_size 1–5 px, osv).

## Teknikval

Jag har tidigare byggt en komplett React Native/Expo-app (SDK 57, Expo Go på
iPhone) så **Expo/React Native är min föredragna stack**. Pipelinen kan inte
köras i numpy på mobilen — föreslå själv den bästa vägen (t.ex.
react-native-skia med shaders, förberäknade 3D-LUT:ar genererade från
desktop-pipelinen, eller en kombination) och motivera valet. Viktigast:
resultatet ska matcha desktop-versionens filmer så nära som möjligt, och
förhandsvisningen ska kännas omedelbar när man bläddrar mellan filmer.

## Funktioner för v1 (mobil)

1. Öppna foto från kamerarullen (och ta nytt foto med kameran)
2. Filmremsa med live-tumnaglar av alla 26 filmer (som desktop)
3. Styrka-reglage (blanda original ↔ graderad, 0–100 %)
4. Justera-panel: exponering, kontrast, mättnad, värme, färgton, fade,
   korn, vinjett, halation (samma skalor som desktop)
5. Spara till kamerarullen i full upplösning
6. Före/efter (håll fingret på bilden = visa original)

**Hoppa över i v1** (finns på desktop, kan komma senare): beskärning/räta upp,
tonkurve-editor, foto-rulle med flera bilder, egna presets, projektfiler,
export-format (alltid JPEG hög kvalitet i v1).

## Design

Behåll desktopens identitet men anpassa till mobil:
- **Mörkt tema som standard** (mobilappar lever i mörkt): palett från
  `THEMES["dark"]` i källkoden (PAPER #211e18, MAT #37332c, INK #ece6d8,
  accent oxblod #b5675c, osv.)
- Serif-typografi för rubriker/filmnamn (som bildtexter i en fotobok),
  fotot monterat med passepartout-känsla
- Filmremsan längst ner som horisontellt scrollbar rad med tumnaglar +
  filmnamn, aktiv film markerad med oxblod-ram
- Appikonen finns i `C:\Claude\filmrulle\filmrulle.icns`/`filmrulle.ico`
  (filmrulle-motiv, krämvit + oxblodsröd på mörk botten)

## Arbetssätt

- Testa mot riktiga foton tidigt; jag kör Expo Go på iPhone
- Verifiera att filmernas look matchar desktop genom att jämföra samma
  testbild i båda apparna
- Fråga mig när designbeslut inte är självklara — jag är produktägare
  och testare
