# zuno_push

[![codecov](https://codecov.io/gh/Zuno-Chat/zuno_push/graph/badge.svg)](https://codecov.io/gh/Zuno-Chat/zuno_push)

Synapse module that rings a closed Zuno app through stock Sygnal pushes (PushKit VoIP on iOS, FCM on Android), and serves the Notification Service Extension's credential, fetch, ring-status and catch-up API. Requests carry the user's Matrix access token, or for the NSE routes a `ZunoNotify` credential the module mints. Endpoints live under `/_synapse/client/zuno/push/v1/`, and every module reply carries `X-Zuno-Push: 1`.

| Route | Purpose |
|---|---|
| `PUT`/`DELETE voip` | Register or drop this device's ring token (PushKit or FCM) and blob key |
| `DELETE device` | Remove everything the module holds for this device |
| `GET health` | This device's pushers, ring registration and NSE credential state |
| `POST test` | A test alert through Synapse's own pusher |
| `POST nse/credential` | Mint the NSE credential (30 days) |
| `POST nse/fetch` | One notified event, plus `missed` and `read_rooms` |
| `POST ring/status` | Long-poll a call's ring outcome for this device |

On the background-tasks instance, a 250 ms poller follows `m.call.member` state deltas, applies Synapse's own power-level and push-rule evaluation, and seals one ChaCha20-Poly1305 blob per device for Sygnal's `/notify`. Every private Synapse call lives in `src/internals.py`. Startup probes gate each feature, and `contract_tests/` pins that code inside Synapse's own test harness at the version in `contract_tests/SYNAPSE_VERSION`.

## Config

The `modules:` entry in `homeserver.yaml`, in every process that serves `/_synapse/client/` and in the background-tasks instance:

```yaml
modules:
  - module: zuno_push.ZunoPush
    config:
      sygnal_notify_url: "http://127.0.0.1:5000/_matrix/push/v1/notify"
      voip_app_ids: ["im.zuno.chat.ios.voip", "im.zuno.chat.ios.dev.voip"]
      fcm_app_ids: ["im.zuno.chat.android"]
      max_ring_targets: 5000
      disabled_features: []
```

- `voip_app_ids` are Sygnal APNs apps and take a base64 PushKit token; `fcm_app_ids` are Sygnal FCM apps and take the raw FCM token. Rings on both carry the constant `event_id` `$z`, because Sygnal drops an FCM push that has no event id, room id or badge counts. `[]` refuses new Android registrations.
- `disabled_features` is the kill switch: any of `ring`, `health`, `test`, `nse` or `catchup` answers 503 `IM.ZUNO.PUSH_DISABLED`.
- A broken config disables the module and leaves Synapse running.

## Develop

- `make check` runs ruff and the unit tests.
- `make coverage` runs the unit tests under coverage.py. CI uploads unit and contract coverage to Codecov, and only the contract tests reach `src/internals.py`.
- `make contract` runs the contract tests inside Synapse's own test harness at the pinned version; set `SYNAPSE_POSTGRES=1` and the `SYNAPSE_POSTGRES_*` variables for the Postgres run.
- Run both suites before moving the Synapse pin, and move it in `contract_tests/SYNAPSE_VERSION`, `src/module.py` and `pyproject.toml` together.

## Deploy check

```sh
ZUNO_SMOKE_TOKEN='<test account access token>' python -m zuno_push.smoke --homeserver https://<homeserver>
```

This prints `OK zuno_push answers on the public URL`, or one `FAIL` line per misrouted path.

## License

Free software under the GNU Affero General Public License, version 3 or any later version. See [LICENSE](LICENSE).
