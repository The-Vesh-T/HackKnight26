-- Questions Tradio asks of Snowflake, across every loaded speech.
-- `python -m tradio.warehouse snowflake-query` runs each block in order.
-- Kalshi only (Polymarket is indicative), and only contracts that were active before T0:
-- a thin contract can only end "reacted" or "can't tell", so it would bias reaction rates.

-- name: Reaction rate by speaker role
SELECT e.role,
       COUNT(DISTINCT e.event_id)                                        AS speeches,
       COUNT(*)                                                          AS active_contracts,
       ROUND(AVG(IFF(r.status IN ('reacted', 'candidate_delay'), 1, 0)), 3) AS reaction_rate
FROM tradio_reactions r
JOIN tradio_events e USING (event_id)
WHERE r.venue = 'kalshi' AND r.liquidity = 'active'
GROUP BY e.role
ORDER BY reaction_rate DESC;

-- name: Reaction rate by stance surprise (|top z|)
SELECT CASE WHEN e.top_z IS NULL THEN 'not scored'
            WHEN ABS(e.top_z) < 1 THEN '|z| < 1'
            WHEN ABS(e.top_z) < 2 THEN '1 <= |z| < 2'
            ELSE '|z| >= 2' END                                          AS surprise,
       COUNT(DISTINCT e.event_id)                                        AS speeches,
       COUNT(*)                                                          AS active_contracts,
       ROUND(AVG(IFF(r.status IN ('reacted', 'candidate_delay'), 1, 0)), 3) AS reaction_rate
FROM tradio_reactions r
JOIN tradio_events e USING (event_id)
WHERE r.venue = 'kalshi' AND r.liquidity = 'active'
GROUP BY surprise
ORDER BY surprise;

-- name: How fast reacting contracts absorbed the move (minutes after T0)
SELECT r.contract,
       COUNT(*)                                     AS reactions,
       ROUND(MEDIAN(r.t50_min), 1)                  AS median_t50_min,
       ROUND(MEDIAN(r.t90_min), 1)                  AS median_t90_min,
       ROUND(MEDIAN(ABS(r.move_ticks)), 1)          AS median_move_ticks
FROM tradio_reactions r
WHERE r.venue = 'kalshi' AND r.status = 'reacted'
GROUP BY r.contract
ORDER BY reactions DESC;

-- name: Learner scoreboard (Brier score, lower is better; test-then-train predictions)
SELECT model,
       COUNT(*)                            AS predictions,
       ROUND(AVG(POWER(p - label, 2)), 4)  AS brier
FROM tradio_predictions
WHERE label IS NOT NULL
GROUP BY model
ORDER BY brier;
