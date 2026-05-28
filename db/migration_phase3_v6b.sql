-- Migration Phase 3 v6b — T1.5: canonical_tags + unknown_tags
-- =========================================================================
-- Cierra T1.5 del plan `docs/fase-3-hardening-pre-batch.md`.
-- Va separada de v6 (T1.4/T1.4b/T1.4c) para que cada feature pueda revertirse
-- de forma independiente si una falla en producción.
--
-- Idempotente: usa IF NOT EXISTS + ON CONFLICT DO NOTHING para el seed.
-- =========================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────
-- Tablas
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS canonical_tags (
    canonical_id  TEXT PRIMARY KEY,        -- kebab-case (e.g. 'dog-friendly')
    aliases       TEXT[] NOT NULL DEFAULT '{}',  -- variantes que mapean al canónico
    category      TEXT,                     -- 'pricing' | 'type' | 'location' | 'atmosphere'
                                            -- | 'services' | 'people' | 'activity' | 'issue' | 'other'
    added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- GIN sobre aliases — para canonicalize_tag() la query es scan completo o
-- carga-en-memoria (lo segundo está implementado en tag_canonicalizer.py),
-- pero el GIN protege si alguien hace WHERE 'gratis' = ANY(aliases) ad-hoc.
CREATE INDEX IF NOT EXISTS idx_canonical_tags_aliases_gin
    ON canonical_tags USING GIN (aliases);

CREATE INDEX IF NOT EXISTS idx_canonical_tags_category
    ON canonical_tags (category) WHERE category IS NOT NULL;

CREATE TABLE IF NOT EXISTS unknown_tags (
    tag               TEXT PRIMARY KEY,    -- raw tag emitido por el LLM (lowercase strip)
    first_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    occurrence_count  INT NOT NULL DEFAULT 1,
    reviewed          BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_unknown_tags_count
    ON unknown_tags (occurrence_count DESC) WHERE reviewed = FALSE;

COMMENT ON TABLE canonical_tags IS
    'T1.5: vocabulario canónico de tags. ingest_v2 filtra parsed.tags por aquí antes de persistir.';
COMMENT ON TABLE unknown_tags IS
    'T1.5: tags emitidos por LLM que no mapean a ningún canonical. Job mensual review_unknown_tags.py los promociona o descarta.';

-- ─────────────────────────────────────────────────────────────────────
-- Seed inicial — vocabulario base
-- ─────────────────────────────────────────────────────────────────────
--
-- Estos canónicos cubren los patrones observados en outputs v3/v4 del LLM y los
-- tags de los few-shots v6. Si el batch detecta tags fuera de este set caen en
-- unknown_tags con frequency tracking para promoción manual mensual (T2.4).
--
-- ON CONFLICT DO NOTHING — la migración es re-aplicable sin duplicar filas.

INSERT INTO canonical_tags (canonical_id, aliases, category) VALUES
    -- ─── Pricing ─────────────────────────────────────────────────────
    ('free',          ARRAY['gratis','gratuit','gratuito','no-charge','no-fee']::TEXT[], 'pricing'),
    ('paid',          ARRAY['pago','pago-required','payant','bezahlt']::TEXT[],         'pricing'),
    ('cheap',         ARRAY['budget','low-cost','barato']::TEXT[],                       'pricing'),
    ('expensive',     ARRAY['pricey','overpriced','caro']::TEXT[],                       'pricing'),

    -- ─── Spot type ───────────────────────────────────────────────────
    ('aire',          ARRAY['aire-ac','aire-de-service','area-camper','area-sosta']::TEXT[], 'type'),
    ('stellplatz',    ARRAY['wohnmobilstellplatz','stell-platz']::TEXT[],                'type'),
    ('camping',       ARRAY['campsite','campground','camping-municipal']::TEXT[],         'type'),
    ('parking',       ARRAY['carpark','car-park','parqueo','parking-lot']::TEXT[],       'type'),
    ('wild',          ARRAY['wild-camping','boondocking','dispersed','off-grid']::TEXT[],'type'),
    ('agriturismo',   ARRAY['farm-stay','agroturismo','agritourisme']::TEXT[],           'type'),
    ('farm',          ARRAY['farmland','rural-farm']::TEXT[],                            'type'),
    ('rest-area',     ARRAY['rest-stop','aire-de-repos','autohof']::TEXT[],              'type'),
    ('marina',        ARRAY['port','harbor','harbour']::TEXT[],                          'type'),

    -- ─── Location / setting ──────────────────────────────────────────
    ('mountain',      ARRAY['montagne','berg','mountainous','alpine']::TEXT[],           'location'),
    ('beach',         ARRAY['playa','plage','strand','seafront','beachfront']::TEXT[],   'location'),
    ('river',         ARRAY['riverside','rio','fluss','rivière','riverbank']::TEXT[],    'location'),
    ('lake',          ARRAY['lakeside','lago','see','lac']::TEXT[],                      'location'),
    ('sea',           ARRAY['seaside','ocean','coast','coastal']::TEXT[],                'location'),
    ('forest',        ARRAY['woodland','bosque','wald','foret']::TEXT[],                 'location'),
    ('urban',         ARRAY['city','ciudad','town','centro']::TEXT[],                    'location'),
    ('rural',         ARRAY['countryside','campo','land','remote-rural']::TEXT[],        'location'),
    ('valley',        ARRAY['valle','tal']::TEXT[],                                       'location'),
    ('viewpoint',     ARRAY['mirador','overlook','panorama','aussicht']::TEXT[],         'location'),
    ('ski-area',      ARRAY['ski-resort','ski-station','skigebiet','estacion-esqui','ski area']::TEXT[], 'location'),
    ('national-park', ARRAY['nationalpark','parque-nacional']::TEXT[],                   'location'),
    ('vineyard',      ARRAY['winery','wine-region','vinedos']::TEXT[],                   'location'),

    -- ─── Atmosphere ──────────────────────────────────────────────────
    ('quiet',         ARRAY['peaceful','tranquilo','calme','silent','still']::TEXT[],    'atmosphere'),
    ('noisy',         ARRAY['loud','ruidoso','bruyant','laut']::TEXT[],                  'atmosphere'),
    ('busy',          ARRAY['crowded','packed','masificado','voll']::TEXT[],             'atmosphere'),
    ('remote',        ARRAY['isolated','aislado','abgelegen','off-the-beaten-path']::TEXT[], 'atmosphere'),
    ('scenic',        ARRAY['picturesque','pretty','beautiful','schön','bello']::TEXT[], 'atmosphere'),
    ('central',       ARRAY['centric','centrico','well-located']::TEXT[],                'atmosphere'),

    -- ─── Services ────────────────────────────────────────────────────
    ('water',         ARRAY['drinking-water','potable-water','agua-potable']::TEXT[],    'services'),
    ('electricity',   ARRAY['power','hookup','strom','elektrik']::TEXT[],                'services'),
    ('dump-station',  ARRAY['grey-water','black-water','vidange','entsorgung','vaciado']::TEXT[], 'services'),
    ('wifi',          ARRAY['wi-fi','internet','wlan']::TEXT[],                          'services'),
    ('shower',        ARRAY['showers','ducha','douche','dusche']::TEXT[],                'services'),
    ('toilet',        ARRAY['wc','public-wc','restroom','aseo','toilette']::TEXT[],      'services'),
    ('laundry',       ARRAY['washing','lavanderia','wäscherei']::TEXT[],                 'services'),
    ('restaurant',    ARRAY['bar','cafe','food']::TEXT[],                                'services'),
    ('pool',          ARRAY['swimming-pool','piscine','piscina']::TEXT[],                'services'),
    ('no-services',   ARRAY['without-services','sin-servicios','no-amenities']::TEXT[],  'services'),
    ('night-lighting',ARRAY['lit','illuminated','iluminado']::TEXT[],                    'services'),
    ('security',      ARRAY['guarded','on-site-security','vigilancia']::TEXT[],          'services'),

    -- ─── People / accessibility ──────────────────────────────────────
    ('dog-friendly',  ARRAY['dogs-allowed','perros','chiens-acceptes','hundefreundlich','pet-friendly','dogs-ok']::TEXT[], 'people'),
    ('family-friendly', ARRAY['families','kids-welcome','niños','familienfreundlich']::TEXT[], 'people'),
    ('accessible',    ARRAY['wheelchair','pmr','barrier-free','accesible']::TEXT[],      'people'),
    ('multilingual',  ARRAY['english-spoken','english-friendly']::TEXT[],                'people'),

    -- ─── Activity ────────────────────────────────────────────────────
    ('mtb',           ARRAY['mountain-biking','vtt','bike-trails']::TEXT[],              'activity'),
    ('hiking',        ARRAY['walking','trekking','randonnée','wandern','senderismo']::TEXT[], 'activity'),
    ('surfing',       ARRAY['surf','surf-spot','windsurf']::TEXT[],                      'activity'),
    ('fishing',       ARRAY['angling','pesca','peche']::TEXT[],                          'activity'),
    ('climbing',      ARRAY['rock-climbing','escalada','klettern']::TEXT[],              'activity'),
    ('cycling',       ARRAY['biking','velo','rad']::TEXT[],                              'activity'),
    ('skiing',        ARRAY['ski','snowboarding','snow-sports']::TEXT[],                 'activity'),
    ('winter-sports', ARRAY['snow','wintersport']::TEXT[],                               'activity'),
    ('birdwatching',  ARRAY['bird-watching','ornithology']::TEXT[],                      'activity'),

    -- ─── Vehicle / access ────────────────────────────────────────────
    ('large-vehicle', ARRAY['big-rig','7m+','large-motorhome','grand-camping-car']::TEXT[], 'other'),
    ('small-vehicle', ARRAY['compact-only','no-big-vehicles']::TEXT[],                    'other'),
    ('off-road',      ARRAY['4x4','rough-road','dirt-road','piste']::TEXT[],              'other'),
    ('paved-access',  ARRAY['tarmac','asphalt','sealed-road']::TEXT[],                    'other'),

    -- ─── Profile / use-case ──────────────────────────────────────────
    ('overnighting',  ARRAY['overnight','pernocta','nuit','overnight-stay']::TEXT[],     'other'),
    ('budget-travel', ARRAY['budget','cheap-travel','low-cost-travel']::TEXT[],          'other'),
    ('seasonal',      ARRAY['summer-only','winter-only','open-seasonally']::TEXT[],      'other'),
    ('winter-friendly', ARRAY['winter-open','snow-ok','winter-stays']::TEXT[],           'other'),

    -- ─── Issues / warnings ───────────────────────────────────────────
    ('construction',  ARRAY['works','obras','chantier','baustelle','bouwput']::TEXT[],   'issue'),
    ('closed',        ARRAY['closed-permanently','cerrado','geschlossen']::TEXT[],       'issue'),
    ('seasonal-closure', ARRAY['winter-closed','summer-closed','closed-off-season']::TEXT[], 'issue'),
    ('avoid',         ARRAY['not-recommended','dont-stay','evitar']::TEXT[],             'issue'),
    ('dirty',         ARRAY['unclean','sucio','sale']::TEXT[],                           'issue'),
    ('mosquitoes',    ARRAY['mosquitos','bugs','moustiques']::TEXT[],                    'issue'),
    ('exposed',       ARRAY['windy','wind-exposed','expuesto']::TEXT[],                  'issue'),
    ('flood-risk',    ARRAY['flooding','riesgo-inundacion']::TEXT[],                     'issue')
ON CONFLICT (canonical_id) DO NOTHING;

COMMIT;

-- ─────────────────────────────────────────────────────────────────────
-- Smoke test
-- ─────────────────────────────────────────────────────────────────────
-- SELECT category, COUNT(*) FROM canonical_tags GROUP BY category ORDER BY 2 DESC;
-- SELECT canonical_id, array_length(aliases, 1) FROM canonical_tags
--   ORDER BY array_length(aliases,1) DESC NULLS LAST LIMIT 10;
