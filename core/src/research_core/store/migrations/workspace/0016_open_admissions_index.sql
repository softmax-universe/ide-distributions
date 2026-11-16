-- work_status reads the admissions that are not settled, and /status asks for it every second.
-- This index holds only those rows, under the same condition work_status tests, so SQLite reads
-- them alone. A table scan read every admission's request, message and payload text: 0.4 s on a
-- store holding 7,000. Change the condition here and in work_status together, or the scan returns.
CREATE INDEX client_admissions_open ON client_admissions(kind)
    WHERE status IN ('queued','running') OR execution_uncertain=1 OR resolution_required=1
        OR advanced_at IS NULL OR disposition='delivery retries exhausted';
