# lyanna-agent

Pipeline vědeckého feedu Lyanna. Sbírá RSS, třídí, píše a překládá články
a zapisuje je do Supabase, odkud je čte web `lyanna-web`.

Nahrazuje Make.com úplně — Make se nezachovává ani neběží dál vedle
téhle pipeline. Frontend ani databázové schéma `articles` se tím nemění,
web o existenci téhle pipeline neví a nemá vědět.

---

## Jak to funguje

```
1. SBĚR      collect_rss.py   72 RSS zdrojů  →  raw_items
2. TRIÁŽ     triage.py        luna, dávky po 30, skóre 0–10 + kategorie
3. DOTAŽENÍ  enrich.py        Jina Reader, jen u položek nad prahem
4. PSANÍ     write.py         terra píše anglicky, terra překládá  →  articles
```

Každá vrstva propouští méně položek a utratí víc na kus. Zhruba
700 nasbíraných titulků denně → ~60 nad prahem → nejvýš 10 napsaných.
Peníze se utrácejí na úzkém konci.

**Pravidlo dvou bran:** na Supabase se sahá výhradně přes `src/db.py`,
na OpenAI výhradně přes `src/ai_client.py`. Nikde jinde v projektu se
přímé volání neobjeví.

---

## První spuštění

### 0. Vyřazení Make.com (nevratné, spustit jen jednou)

V databázi dnes existuje tabulka `raw_items`, kterou plní běžící
Make.com scénář (sloupce `link`, `feed_source` místo `url`, `source`) —
a `articles` obsahuje texty, které napsal Make. Chceš čistý start, ne
souběh, takže se to smaže.

**Nejdřív v samotném Make.com** deaktivuj nebo smaž scénáře pro Lyannu —
tenhle repozitář na Make.com nemá žádný přístup, vypnutí musí proběhnout
tam. Bez tohohle kroku by Make dál běžel na pozadí a stál kredity, i
když by narazil na smazanou tabulku.

**Pak v Supabase → SQL Editor** spusť `sql/000_cleanup_make.sql`. Vyprázdní
`articles` (web bude bez obsahu, dokud neproběhne první běh nové
pipeline — viz krok 4 níž) a zahodí starou tabulku `raw_items`. Skript
má na začátku poznámku, jak si obsah `articles` předem zazálohovat
(export CSV v Table Editoru), kdyby ses k němu chtěl někdy vrátit.

### 1. Databáze

V Supabase → SQL Editor spusť `sql/001_init.sql`. Založí čistou tabulku
`raw_items` s unikátem na URL a přidá do `articles` sloupce `pipeline`
a `raw_item_id`. Skript je idempotentní, opakované spuštění nic
nerozbije — ale musí běžet **až po** kroku 0, jinak narazí na starou
tabulku s jinými sloupci.

### 2. Klíče

V GitHubu → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Odkud |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → API |
| `SUPABASE_SERVICE_KEY` | tamtéž, klíč **service_role** |
| `OPENAI_API_KEY` | platform.openai.com |
| `JINA_API_KEY` | volitelné, zatím netřeba |

> `SUPABASE_SERVICE_KEY` obchází RLS. Patří výhradně do GitHub Secrets,
> nikdy do souboru v repozitáři a nikdy do frontendu. Web zůstává
> u publishable klíče, tak jak je dnes.

Lokálně: `cp .env.example .env` a vyplnit. `.env` je v `.gitignore`.

### 3. Diagnostika

GitHub → Actions → **Diagnostika** → Run workflow.

Ověří konfiguraci, Supabase (čtení i zápis), oba modely, Jina Reader
a všech 72 feedů. Výstupem je tabulka. **Dokud v ní svítí CHYBA,
pipeline nespouštěj.**

Lokálně totéž: `pip install -r requirements.txt && python diagnose.py`

### 4. První ostrý běh

GitHub → Actions → **Denní běh** → Run workflow.

Pak zkontroluj dvě věci:

```sql
-- Sběr funguje a idempotence drží: druhé spuštění hned po prvním
-- nesmí přidat ani jeden nový řádek.
select count(*), max(collected_at) from raw_items;

-- Rozhodnutí triáže — projdi jich ručně dvacet.
select score, category, reason, title, source
  from raw_items where status in ('scored','waiting')
 order by score desc limit 20;
```

Podle jízdního plánu je fáze 2 hotová, když s alespoň 17 z 20 rozhodnutí
souhlasíš. Když ne, ladí se `config/prompts/triage.md` a definice
v `config/taxonomy.yaml` — ne kód.

---

## Kde se co mění

Chování se mění v `config/`, ne v `.py` souborech.

| Chci | Kde |
|---|---|
| přidat/odebrat zdroj | `config/sources.yaml` — jeden řádek |
| změnit práh skóre | `config/settings.yaml` → `triage.score_min` |
| změnit denní strop článků | `settings.yaml` → `write.max_articles_per_day` |
| změnit model | `settings.yaml` → `triage.model` / `write.model` |
| změnit styl psaní | `config/prompts/writer.md` |
| upřesnit kategorie | `config/taxonomy.yaml` |
| zúžit objem bez mazání zdrojů | `settings.yaml` → `sources.enabled_tiers: [core]` |

---

## Stavy položek v `raw_items`

| Stav | Znamená |
|---|---|
| `new` | nasbíráno, čeká na triáž |
| `scored` | nad prahem, čeká na zpracování |
| `waiting` | čekárna (skóre 4–7) — nezahazuje se, ale ani nezpracovává |
| `rejected` | pod čekárnou |
| `duplicate` | táž zpráva už prošla odjinud; `duplicate_of` ukazuje na originál |
| `used` | zpracováno do článku |
| `failed` | zpracování selhalo, nezkouší se donekonečna |

Nezhodnocená položka zůstává ve stavu `new` a zkusí se v dalším běhu.
Nic se neztrácí.

---

## Odstranění duplicit napříč zdroji

Jedna studie dorazí z phys.org, ScienceDaily, New Scientist i Guardianu
jako čtyři různé URL. Unikát na URL to nezachytí. Řeší se ve dvou vrstvách:

1. **Kód** (`src/dedup.py`) porovná slovní kmeny titulků. Chytá běžné
   přeformulování, měřeno na sadě reálných dvojic.
2. **Model** při triáži vidí celý blok třiceti titulků a má instrukci
   označit dvojice, které kód minul (typicky „61 days" versus
   „two months", „JWST" versus „Webb telescope").

Ostatní pokrytí téže zprávy se nezahazují — připojí se k výslednému
článku jako další položky v `articles.sources`, takže čtenář má odkazy
na všechna.

---

## Make.com

Make se nezachovává jako záloha ani neběží dál vedle téhle pipeline —
podle rozhodnutí bylo úplně nahrazeno, ne dočasně doplněno. Krok 0 výš
smaže jeho data v Supabase; scénáře samotné se vypínají přímo v Make.com
rozhraní, což je mimo dosah tohohle repozitáře.

Návratová cesta, kdyby nová pipeline zklamala, teď vede přes Git
(`git revert` na commit, který ji zavedl) a znovunastavení scénářů
v Make.com, ne přes jedno tlačítko — to je vědomá cena za čistý start
bez souběhu dvou zdrojů dat.

---

## Co tenhle repozitář zatím neumí

Fáze 5–7 z jízdního plánu, tedy trvalá vrstva:

- `library` — PDF do databáze jako text s abstraktem a tagy
- `topics` — týdenní destilace proudu do trvalých témat
- Frontiers na webu čtoucí z `topics` místo statického objektu v `App.jsx`

Podle jízdního plánu se k nim jde, až proud běží stabilně a jsi spokojený
s kvalitou textů. Zásoba postavená nad nefungujícím proudem jen znásobuje
problém.

---

## Poznámky k provozu

- **GitHub vypíná cron v neaktivních repozitářích** po ~60 dnech bez
  aktivity. U agenta, který má běžet roky bez zásahu, je to reálné riziko.
- **Strop tokenů je pojistka, ne rozpočet.** Platí se za vygenerované
  tokeny, ne za nevyčerpaný strop. Snižovat ho „kvůli úspoře" znamená
  riskovat prázdný výstup u objemnějšího vstupu — reasoning modely
  spotřebují část stropu neviditelným přemýšlením.
- **HTTP 200 neznamená úspěch obsahu.** `ai_client.py` proto kontroluje,
  že text není prázdný, a při prázdném výstupu loguje důvod ukončení
  i počet přemýšlecích tokenů.
