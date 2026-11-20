ALTER TABLE semora_meta.sessions
    ADD COLUMN IF NOT EXISTS owner_subject VARCHAR(200);

CREATE INDEX IF NOT EXISTS sessions_owner_created_idx
    ON semora_meta.sessions(owner_subject, created_at DESC);

UPDATE semora_meta.sessions AS session
SET owner_subject = inferred.owner_subject
FROM (
    SELECT session_id, min(payload #>> '{authorization,subject}') AS owner_subject
    FROM semora_meta.runs
    WHERE payload #>> '{authorization,subject}' IS NOT NULL
    GROUP BY session_id
    HAVING count(DISTINCT payload #>> '{authorization,subject}') = 1
) AS inferred
WHERE session.id = inferred.session_id
  AND session.id <> 'demo-session'
  AND session.owner_subject IS NULL;

UPDATE semora_meta.sessions
SET owner_subject = 'legacy-unowned'
WHERE owner_subject IS NULL AND id <> 'demo-session';
