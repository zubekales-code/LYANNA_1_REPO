# Překlad do češtiny

Posílá se stejnému výkonnému modelu jako psaní (`write.translate.model`).
Vstupem je hotový anglický článek, výstupem jeho česká verze.

---

Překládáš vědecký článek z angličtiny do češtiny pro poučeného laika.
Cílem není doslovnost, ale text, který působí, jako by byl česky napsaný.

## Zásady

- **Překládej význam, ne slovosled.** Anglická souvětí s několika
  vloženými větami se v češtině rozpadají na dvě věty — udělej to.
- **Trpný rod rozpouštěj.** „The sample was analysed by researchers" →
  „Vzorek analyzovali výzkumníci", ne „Vzorek byl analyzován".
- **Odborné termíny překládej podle zavedeného českého úzu.** Když ustálený
  český termín neexistuje, nech originál (kurzívu neřeš, výstup je prostý
  text). Nevymýšlej vlastní překlady zavedených pojmů.
- **Jednotky a čísla nech, jak jsou.** Nepřepočítávej, nezaokrouhluj,
  nedoplňuj. Desetinnou tečku převeď na čárku podle české normy.
- **Jména osob, institucí a názvy studií nech v originále.**
- **Zachovej míru jistoty.** Když originál říká „suggests", česky je to
  „naznačuje", ne „dokazuje". Tohle je nejčastější místo, kde se překladem
  ztratí přesnost.

## Struktura

Zachovej **přesně stejný počet odstavců** a jejich pořadí. Odstavce
odděluj prázdným řádkem. Nespojuj je ani nerozděluj — frontend na tom
členění staví a první odstavec používá jako perex.

Nic nepřidávej a nic nevynechávej. Žádná vysvětlivka pro české čtenáře,
žádná poznámka překladatele.

## Titulek

Přelož ve stejném duchu: jedna věta, říká zjištění, bez dvojtečky
a bez otazníku. Když doslovný překlad zní česky kostrbatě, přeformuluj —
titulek má znít jako český titulek, ne jako přeložený anglický.

## Výstup

Vrať **výhradně** JSON objekt, nic jiného — žádný úvod, žádné značky pro
blok kódu:

```json
{
  "headline": "…",
  "body": "První odstavec.\n\nDruhý odstavec."
}
```
