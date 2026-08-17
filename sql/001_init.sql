-- ---------------------------------------------------------------------------
-- Lyanna agent — inicializační migrace
--
-- Spustit JEDNOU v Supabase → SQL Editor, před prvním během pipeline.
-- Celý skript je idempotentní: opakované spuštění nic nerozbije.
--
-- Tabulka `articles` už existuje a tenhle skript ji nemění nad rámec
-- přidání dvou sloupců, které nic nerozbíjejí (frontend dělá select("*"),
-- takže sloupce navíc jsou pro web neviditelné).
-- ---------------------------------------------------------------------------


-- ---------------------------------------------------------------------------
-- 1. PROUD: efemérní vrstva mezi RSS a hotovým článkem
--
-- Předpokládá se, že jsi PŘED tímhle skriptem spustil `sql/000_cleanup_make.sql`
-- a starou tabulku `raw_items` z Make.com scénáře tím pádem zahodil. Pokud
-- ne, tenhle příkaz spadne na "already exists" u sloupců, které stará
-- tabulka nemá — viz 000_cleanup_make.sql.
--
-- Její jediný smysl je idempotence: unique na URL znamená, že pipeline
-- jde pustit desetkrát za sebou a nevzniknou duplicity.
-- ---------------------------------------------------------------------------
create table if not exists raw_items (
    id            bigint generated always as identity primary key,
    url           text not null,
    title         text,
    description   text,
    source        text,                       -- název zdroje ze sources.yaml
    published_at  timestamptz,
    collected_at  timestamptz default now(),

    -- výsledek triáže
    score         int,
    category      text,
    reason        text,                       -- krátké zdůvodnění, pro ruční kontrolu

    -- new      = nasbíráno, nezhodnoceno
    -- scored   = zhodnoceno, čeká nad prahem na zpracování
    -- waiting  = čekárna (skóre 4–7)
    -- rejected = pod čekárnou
    -- duplicate= táž zpráva už prošla z jiného zdroje
    -- used     = zpracováno do článku
    -- failed   = zpracování selhalo, nezkoušet znovu donekonečna
    status        text default 'new',

    -- normalizovaný otisk titulku, slouží k odhalení téže zprávy
    -- přicházející z různých zdrojů pod různými URL
    cluster_key   text,

    -- id položky, ke které byla tahle připojena jako duplicita
    duplicate_of  bigint references raw_items(id) on delete set null
);

-- Klíč idempotence. Bez něj nemá `on_conflict=url` o co se opřít
-- a hromadný zápis s ignore-duplicates tiše nefunguje.
create unique index if not exists raw_items_url_key on raw_items (url);

-- Triáž se ptá na status='new', zpracování na status='scored' seřazené
-- podle skóre. Obojí bez indexu při desítkách tisíc řádků zpomalí.
create index if not exists raw_items_status_idx        on raw_items (status);
create index if not exists raw_items_status_score_idx  on raw_items (status, score desc);
create index if not exists raw_items_cluster_key_idx   on raw_items (cluster_key);
create index if not exists raw_items_collected_at_idx  on raw_items (collected_at desc);


-- ---------------------------------------------------------------------------
-- 2. Rodokmen článku
--
-- `pipeline` byl původně určený k rozlišení souběhu se starou Make.com
-- pipeline. Make se ale nezachovává — starý obsah `articles` se maže
-- v `000_cleanup_make.sql` a od teď píše výhradně tenhle repozitář.
-- Sloupec zůstává jako obecný rodokmen (pro případ dalšího zdroje obsahu
-- v budoucnu) a jako historická informace, kdyby ses někdy vracel
-- k zálohovaným datům.
--
-- `raw_item_id` je zpětná vazba na zdrojovou položku — užitečné při
-- ladění promptů, protože od hotového článku se dostaneš k tomu,
-- co do něj vstupovalo.
-- ---------------------------------------------------------------------------
alter table articles add column if not exists pipeline text default 'python';

create index if not exists articles_pipeline_idx on articles (pipeline);

alter table articles add column if not exists raw_item_id bigint references raw_items(id) on delete set null;


-- ---------------------------------------------------------------------------
-- 3. Kontrola
-- ---------------------------------------------------------------------------
-- select column_name, data_type
--   from information_schema.columns
--  where table_name = 'raw_items'
--  order by ordinal_position;
