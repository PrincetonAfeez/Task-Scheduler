-- Task Scheduler database schema reference
-- Target: PostgreSQL
-- Note: Django migrations remain the executable source of truth.

CREATE TABLE IF NOT EXISTS scheduler_app_job (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(160) NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    registered_task_name VARCHAR(160) NOT NULL,
    schedule_type VARCHAR(24) NOT NULL CHECK (schedule_type IN ('one_time', 'interval', 'cron')),
    schedule_value JSONB NOT NULL DEFAULT '{}'::jsonb,
    timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at TIMESTAMPTZ NULL,
    last_run_at TIMESTAMPTZ NULL,
    overlap_policy VARCHAR(16) NOT NULL DEFAULT 'skip' CHECK (overlap_policy IN ('skip', 'queue', 'allow')),
    misfire_policy VARCHAR(16) NOT NULL DEFAULT 'coalesce' CHECK (misfire_policy IN ('coalesce', 'catch_up', 'skip')),
    misfire_grace_seconds INTEGER NOT NULL DEFAULT 60 CHECK (misfire_grace_seconds >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CONSTRAINT job_max_attempts_positive CHECK (max_attempts > 0),
    retry_backoff_seconds INTEGER NOT NULL DEFAULT 10 CONSTRAINT job_retry_backoff_nonnegative CHECK (retry_backoff_seconds >= 0),
    timeout_seconds INTEGER NOT NULL DEFAULT 30 CONSTRAINT job_timeout_positive CHECK (timeout_seconds > 0),
    retention_count INTEGER NOT NULL DEFAULT 500 CHECK (retention_count >= 0),
    retention_days INTEGER NOT NULL DEFAULT 30 CHECK (retention_days >= 0),
    alert_mode VARCHAR(16) NOT NULL DEFAULT 'web' CHECK (alert_mode IN ('log_only', 'web')),
    task_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS job_enabled_next_run_idx
    ON scheduler_app_job (enabled, next_run_at);

CREATE TABLE IF NOT EXISTS scheduler_app_jobexecution (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES scheduler_app_job(id) ON DELETE CASCADE,
    scheduled_for TIMESTAMPTZ NULL,
    run_after TIMESTAMPTZ NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending', 'claimed', 'running', 'succeeded', 'failed',
            'retry_scheduled', 'timed_out', 'missed', 'cancelled', 'dead_lettered'
        )
    ),
    attempt_number INTEGER NOT NULL DEFAULT 1 CONSTRAINT execution_attempt_positive CHECK (attempt_number > 0),
    idempotency_key VARCHAR(220) NOT NULL UNIQUE,
    is_manual BOOLEAN NOT NULL DEFAULT FALSE,
    claimed_by VARCHAR(160) NOT NULL DEFAULT '',
    claimed_at TIMESTAMPTZ NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    started_at TIMESTAMPTZ NULL,
    finished_at TIMESTAMPTZ NULL,
    duration_ms INTEGER NULL CHECK (duration_ms IS NULL OR duration_ms >= 0),
    output TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    worker_id VARCHAR(160) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS execution_status_run_idx
    ON scheduler_app_jobexecution (status, run_after);

CREATE INDEX IF NOT EXISTS execution_job_sched_idx
    ON scheduler_app_jobexecution (job_id, scheduled_for);

CREATE INDEX IF NOT EXISTS execution_lease_idx
    ON scheduler_app_jobexecution (lease_expires_at);

CREATE INDEX IF NOT EXISTS execution_job_created_idx
    ON scheduler_app_jobexecution (job_id, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS unique_scheduled_occurrence
    ON scheduler_app_jobexecution (job_id, scheduled_for)
    WHERE scheduled_for IS NOT NULL AND is_manual = FALSE;

CREATE TABLE IF NOT EXISTS scheduler_app_jobevent (
    id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(32) NOT NULL CHECK (
        event_type IN (
            'due_detected', 'occurrence_created', 'occurrence_exists', 'claim',
            'dispatch', 'worker_start', 'worker_finish', 'failure', 'retry_scheduled',
            'timeout', 'misfire', 'lease_recovery', 'stale_claim_rejected',
            'stale_result_discarded', 'manual_retry', 'dead_letter', 'alert',
            'cache_invalidation', 'cancelled', 'operator_action'
        )
    ),
    job_id BIGINT NULL REFERENCES scheduler_app_job(id) ON DELETE CASCADE,
    execution_id BIGINT NULL REFERENCES scheduler_app_jobexecution(id) ON DELETE CASCADE,
    message TEXT NOT NULL DEFAULT '',
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS event_type_created_idx
    ON scheduler_app_jobevent (event_type, created_at);

CREATE INDEX IF NOT EXISTS event_job_created_idx
    ON scheduler_app_jobevent (job_id, created_at);

CREATE INDEX IF NOT EXISTS event_exec_created_idx
    ON scheduler_app_jobevent (execution_id, created_at);

CREATE TABLE IF NOT EXISTS scheduler_app_deadletter (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES scheduler_app_job(id) ON DELETE CASCADE,
    execution_id BIGINT NOT NULL UNIQUE REFERENCES scheduler_app_jobexecution(id) ON DELETE CASCADE,
    reason VARCHAR(160) NOT NULL,
    final_error TEXT NOT NULL DEFAULT '',
    attempts_used INTEGER NOT NULL DEFAULT 1 CHECK (attempts_used > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scheduler_app_alert (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NULL REFERENCES scheduler_app_job(id) ON DELETE CASCADE,
    execution_id BIGINT NULL REFERENCES scheduler_app_jobexecution(id) ON DELETE CASCADE,
    severity VARCHAR(16) NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'error')),
    message TEXT NOT NULL,
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS alert_resolved_created_idx
    ON scheduler_app_alert (resolved, created_at);

CREATE TABLE IF NOT EXISTS scheduler_app_schedulerheartbeat (
    id BIGSERIAL PRIMARY KEY,
    scheduler_id VARCHAR(160) NOT NULL UNIQUE,
    hostname VARCHAR(160) NOT NULL,
    process_id INTEGER NOT NULL CHECK (process_id >= 0),
    last_tick_at TIMESTAMPTZ NOT NULL,
    recent_occurrences_created INTEGER NOT NULL DEFAULT 0 CHECK (recent_occurrences_created >= 0),
    recent_failure_count INTEGER NOT NULL DEFAULT 0 CHECK (recent_failure_count >= 0),
    health_state VARCHAR(32) NOT NULL DEFAULT 'starting',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scheduler_app_workerheartbeat (
    id BIGSERIAL PRIMARY KEY,
    worker_id VARCHAR(160) NOT NULL UNIQUE,
    hostname VARCHAR(160) NOT NULL,
    process_id INTEGER NOT NULL CHECK (process_id >= 0),
    last_heartbeat_at TIMESTAMPTZ NOT NULL,
    active_execution_count INTEGER NOT NULL DEFAULT 0 CHECK (active_execution_count >= 0),
    completed_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    current_execution_id BIGINT NULL REFERENCES scheduler_app_jobexecution(id) ON DELETE SET NULL,
    health_state VARCHAR(32) NOT NULL DEFAULT 'starting',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
