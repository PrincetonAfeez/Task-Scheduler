# Schema

Reference schema files for the Task Scheduler project.

These files mirror the current Django models in `scheduler_app/models.py` and are intended for documentation, database review, integration planning, and API payload validation. Django migrations remain the source of truth for applying database changes.

## Files

- `database_schema.sql` - PostgreSQL-style DDL for the scheduler tables, constraints, indexes, and relationships.
- `job.schema.json` - JSON Schema for a scheduler job payload, including schedule type rules.
- `job_execution.schema.json` - JSON Schema for execution/run records.
- `job_event.schema.json` - JSON Schema for event/audit records.
- `dead_letter.schema.json` - JSON Schema for dead-letter records.
- `alert.schema.json` - JSON Schema for operator alerts.
- `heartbeat.schema.json` - JSON Schema for scheduler and worker heartbeat records.

## Notes

- Datetime fields are represented as ISO 8601 strings in JSON schemas.
- PostgreSQL is the authoritative durable store for jobs, executions, history, retries, leases, alerts, and heartbeat records.
- Redis is intentionally not represented here because it is used as a cache/acceleration layer, not as the source of truth.
