# Blink API notes

What Bling Reborn knows about the Blink endpoints it uses. Blink has no public API; this comes from
[blinkpy](https://github.com/fronzbot/blinkpy), from watching responses, and from the Blink Android app
(version 62.0, November 2026). Endpoints change without notice.

All examples use stand-in IDs.

## Sign-in

OAuth 2.0 with PKCE at `https://api.oauth.blink.com/`, handled by blinkpy. Access tokens last about an
hour; Bling Reborn refreshes them and stores the rotating refresh token (DPAPI-encrypted on Windows).
Requests send `Authorization: Bearer <access token>`.

`GET https://rest-<tier>.immedia-semi.com/api/v1/users/tier_info` gives the account ID and the tier
(region host) used below.

## Clip history: EVM (current, since 19 November 2026)

Blink moved clip storage to Ring's event-video service ("EVM"). The Blink app reaches it through Blink's API
gateway, `https://api.blink.com/blink/` (the app's template is `https://api.{env}blink.com/blink/`, with an
empty `{env}` in production, the same as the sign-in host).

Every gateway request sends:

| Header | Value |
|---|---|
| `Authorization` | `Bearer <access token>` |
| `X-Org-Id` | `Blink` |
| `x-api-no-file-concat` | `true` (on history requests) |

### List clips

`GET evm/v2/history/devices`

| Query | |
|---|---|
| `start_time`, `end_time` | ISO-8601 UTC, e.g. `2026-11-22T18:00:00Z` |
| `limit` | page size (the app's default is 20) |
| `pagination_key` | from the previous page |
| `source_ids` | optional, comma-separated device IDs (EVM IDs; Blink keeps these as `ring_device_id` per camera) |
| `event_types`, `detection_types`, `favorites_only` | optional filters |

Response (abridged):

```json
{
  "items": [
    {
      "event_id": "…",
      "source_id": "2001",
      "start_time": "2026-11-22T18:30:00Z",
      "end_time": "…",
      "event_type": "motion",
      "is_watched": false,
      "device": {"id": 2001, "type": "camera", "description": "Front Door"},
      "visualizations": {
        "cloud_media_visualization": {
          "media": [
            {"file_family": "THUMBNAIL", "url": "https://…"},
            {"file_family": "VIDEO", "file_type": "VIDEO", "is_e2ee": false, "url": "https://…"}
          ]
        }
      }
    }
  ],
  "pagination_key": "…"
}
```

The `url` values are **pre-signed** links: fetch them with a plain GET and **no** `Authorization` header.
They expire; re-fetch the event to get a fresh one.

### Other EVM calls

| Call | |
|---|---|
| `GET evm/v2/history/events/{event_id}` | one event (fresh clip links) |
| `POST evm/v2/events/watch` body `{"event_ids": ["…"]}` | mark clips as watched |
| `GET evm/v2/history/unwatched/count` | unwatched count |
| `GET evm/v3/history/devices` | newer feed format (same idea, `order` instead of `limit`) |
| `POST evm/v2/events` | delete several events |
| `DELETE evm/v2/events/time-based-deletion/{source_id}?start_timestamp=…&end_timestamp=…` | delete a time range |

## Clip history: legacy `media/changed`

`GET https://rest-<tier>.immedia-semi.com/api/v1/accounts/{account_id}/media/changed?since=<time>&page=<n>`

- `since` is `YYYY-MM-DDTHH:MM:SS+0000`; send the `+` URL-encoded (`%2B`). Omitting it gives HTTP 400
  "Bad Time Format".
- Pages are 1-based (page 0 returns the same as page 1); `limit` is 200 per page.
- Each entry has `id`, `created_at`, `device_name`, `network_id`, `watched`, `deleted` and `media`, a path
  under `/api/v3/media/accounts/…/<id>.mp4` fetched from the same host with the Bearer token.
- Stopped receiving new clips on 19 November 2026 for at least some accounts (all entries then showed an
  `updated_at` of 20 November). Bling Reborn still reads it, for older clips.
- `api/v2/accounts/{account_id}/media/changed` also answers, with extra `event_purge_id` and
  `media_purge_id` fields, but listed no newer clips either.

## Snapshots

Bling Reborn asks each camera for a new thumbnail (blinkpy's `request_new_image`), waits for the command to
finish, then downloads the camera's thumbnail.
