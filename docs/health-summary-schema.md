# Health Summary Schema

The canonical health summary contract is the return value of
`HealthCheckMonitor.get_summary()`.

## Required Top-Level Fields

| Field | Type |
|-------|------|
| `sources` | object |
| `bots` | object |
| `overall` | string |

## Source Summary Entry

Each `sources[<source_name>]` entry must include:

| Field | Type |
|-------|------|
| `status` | string |
| `error_count` | int |
| `last_success` | ISO 8601 string or `null` |
| `last_error` | ISO 8601 string or `null` |

## Bot Summary Entry

Each `bots[<bot_name>]` entry must include:

| Field | Type |
|-------|------|
| `status` | string |
| `last_heartbeat` | ISO 8601 string or `null` |

## Overall Status Values

- `healthy`
- `degraded`
- `critical`

This contract feeds the dashboard and operator tooling, so field removals or
renames should be treated as breaking changes.
