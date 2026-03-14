# Verification Record Schema

## Forecast Verification State

`data/weather-verification.json` and `data/weather-nws-cross-check.json` are
versioned state files with this shared top-level shape:

| Field | Type | Notes |
|-------|------|-------|
| `artifact_type` | string | Contract id for the artifact |
| `schema_version` | int | Phase 0 version is `1` |
| `pending` | array | Unresolved observations waiting for settlement |
| `verified` | array | Resolved observations with settlement-aligned actuals |
| `stats` | object | Aggregate metadata such as `last_verification` |

## Weather Forecast Pending Record

Required fields for `ForecastVerifier.record_forecast()` pending rows:

| Field | Type |
|-------|------|
| `city` | string |
| `date` | `YYYY-MM-DD` string |
| `models` | object |
| `record_kind` | `snapshot` or `market` |
| `recorded_at` | ISO 8601 string |

## Weather Forecast Verified Record

Required fields once a forecast has been resolved:

| Field | Type |
|-------|------|
| `city` | string |
| `date` | `YYYY-MM-DD` string |
| `models` | object |
| `actual_high` | number |
| `errors` | object |
| `record_kind` | `snapshot` or `market` |
| `recorded_at` | ISO 8601 string |
| `verified_at` | ISO 8601 string |

Common optional fields:

- `actual_source`
- `threshold`
- `direction`
- `model_prob`
- `per_model_probs`
- `hour_of_day`

## NWS Cross-Check Pending Record

Required fields for `NWSCrossCheckVerifier.record_comparison()` pending rows:

| Field | Type |
|-------|------|
| `city` | string |
| `date` | `YYYY-MM-DD` string |
| `open_meteo_temp` | number |
| `nws_temp` | number |
| `open_meteo_minus_nws` | number |
| `mode` | string |
| `recorded_at` | ISO 8601 string |

## NWS Cross-Check Verified Record

Required fields once a comparison is resolved against settlement:

| Field | Type |
|-------|------|
| `city` | string |
| `date` | `YYYY-MM-DD` string |
| `open_meteo_temp` | number |
| `nws_temp` | number |
| `open_meteo_minus_nws` | number |
| `mode` | string |
| `actual_high` | number |
| `open_meteo_error` | number |
| `nws_error` | number |
| `winner` | string |
| `verified_at` | ISO 8601 string |

Common optional fields:

- `actual_source`
- `days_out`
- `threshold_f`
- `open_meteo_models`
- `model_run_tags`
