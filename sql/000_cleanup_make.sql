-- ---------------------------------------------------------------------------
-- Lyanna agent — vyřazení Make.com
--
-- NEVRATNÉ. Spustit RUČNĚ, JEDNOU, PŘED `001_init.sql`, a jen tehdy,
-- když už sis jistý, že Make.com nemá dál běžet.
--
-- Než tohle spustíš:
--   1. V Make.com samotném deaktivuj nebo smaž scénáře pro Lyannu.
--      Tenhle skript maže jen data v Supabase — Make.com běží dál na
--      vlastní platformě, dokud ho nevypneš tam.
--   2. (Volitelné, ale doporučené.) V Supabase → Table Editor →
--      articles → Export → CSV, pokud by ses někdy chtěl ke starým
--      textům vrátit. Po spuštění skriptu jsou nenávratně pryč.
--
-- Co skript dělá:
--   - Vyprázdní `articles` — web bude bez obsahu, dokud neproběhne
--     první běh nové pipeline (viz README, sekce "První ostrý běh").
--   - Zahodí starou tabulku `raw_items`, kterou plnil Make.com scénář
--     (sloupce id/category/title/link/description/published_at/
--     feed_source/collected_at). Je to jen efemérní staging vrstva —
--     podle principu PROUD v META-ARCHITEKTUŘE je v pořádku o ni přijít.
--
-- Po tomhle skriptu spusť `001_init.sql`, který založí čistou tabulku
-- `raw_items` pro novou pipeline.
-- ---------------------------------------------------------------------------

-- Vyprázdnění obsahu webu. TRUNCATE místo DELETE — u prázdné/téměř
-- prázdné tabulky nezáleží na rychlosti, ale je to explicitnější gesto.
truncate table articles;

-- Zahození staré Make.com staging tabulky. CASCADE by tu nic navíc
-- neodstranil (na `raw_items` nic neodkazovalo, dokud jsme na ni sami
-- nepřidali `articles.raw_item_id` — a ten se přidává až v 001_init.sql,
-- po tomhle kroku), ale je tu pro jistotu, kdyby si to Make.com scénář
-- v Supabase propojil jinak, než čekáme.
drop table if exists raw_items cascade;

-- ---------------------------------------------------------------------------
-- Kontrola
-- ---------------------------------------------------------------------------
-- select count(*) from articles;                          -- očekává se 0
-- select to_regclass('public.raw_items');                 -- očekává se NULL
