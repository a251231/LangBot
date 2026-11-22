# WeChatFerry EBA Adapter

## Structure

WeChatFerry / WeChat 4.x Helper Native Bridge is implemented under `src/langbot/pkg/platform/adapters/wechatferry/` following the standard Event-Based Agents adapter layout:

- `adapter.py`: owns lifecycle (`run_async`, `kill`), reconnectable WebSocket listener (64 MiB default max frame size for inline Base64 media), bounded FIFO deduplication, listener dispatch (most-specific EBA listener plus `to_legacy_event()` compatibility for pipeline bindings), and ordered outbound `send_message` / `reply_message`.
- `event_converter.py`: maps inbound gateway JSON payloads to `MessageReceivedEvent` (`message.received`) and non-message gateway notifications to `PlatformSpecificEvent` (`platform.specific`), while filtering self (`bot_wxid`) and system accounts (`gh_*`, `weixin`, `newsapp`, `fmessage`, `medianote`, `qmessage`).
- `message_converter.py`: converts between `MessageChain` (`Source`, `Plain`, `At`, `AtAll`, `Image`, `Voice`, `File`, `Quote`, `Face`, `Forward`, `Unknown`) and WeChatFerry / Helper V4-V7 payloads, including inline `<img_b64>` Base64 extraction and cross-host Windows drive-path filtering.
- `api_impl.py`: implements optional common EBA lookup APIs against the Helper HTTP gateway (`/api/user_name`, `/api/contacts`, `/api/chatroom_members`) and raises `NotSupportedError` for all unsupported common APIs.
- `platform_api.py`: exposes gateway-specific pass-through actions (`get_status`, `get_friend_requests`, `accept_friend`, `send_rich_text`) via `call_platform_api`.
- `manifest.yaml`: declares adapter ID `wechatferry`, config schema, `supported_events`, `supported_apis`, and `platform_specific_apis`.

## Configuration

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `api_url` | Yes | `http://127.0.0.1:18080` | HTTP base URL of the WeChatFerry / Helper V4+ gateway service. |
| `ws_url` | Yes | `ws://127.0.0.1:18080/ws` | WebSocket URL streaming inbound messages and gateway events. |
| `bot_wxid` | No | `""` | Authentic `wxid` of the logged-in bot account; auto-synced from `GET /api/status` on startup when logged in. |
| `ws_max_size_mb` | No | `64` | Maximum WebSocket frame size in MiB (minimum 16) for inline Base64 images, voice, and files. |

## Events

| Event | Status | Evidence Level | Notes |
|-------|--------|----------------|-------|
| `message.received` | Implemented | `unit`, `plugin-e2e-protocol` (loopback); `blocked` (`plugin-e2e-ui`) | Text, `@` mentions, `@所有人`, inline Base64/URL/path images, voice (`type=34`), file (`type=49`), quote metadata, and unknown fallback. |
| `platform.specific` | Implemented | `unit` | Non-message gateway notifications carrying `event_type != "message"` are emitted as `action="wechatferry.<event_type>"`. |
| `message.edited` | Not supported | `not-supported` | WeChat PC / Helper protocol has no message edit event. |
| `message.deleted` | Not supported | `not-supported` | Recall/unsend event is not exposed as a standard event in the current gateway schema. |
| `message.reaction` | Not supported | `not-supported` | WeChat PC does not expose message reactions. |
| `group.member_joined` / `group.member_left` / `group.member_banned` / `group.info_updated` | Not supported | `not-supported` | Not exposed as structured events in the current Helper WS stream. |
| `friend.request_received` / `friend.added` / `friend.removed` | Not supported | `not-supported` | Pending requests are queried via `call_platform_api("get_friend_requests")` rather than pushed as a standard event. |
| `bot.invited_to_group` / `bot.removed_from_group` / `bot.muted` / `bot.unmuted` | Not supported | `not-supported` | Not exposed by the gateway protocol. |

## Message Component Coverage

### Inbound (`target2yiri`)

| Component | Evidence Level | Notes |
|-----------|----------------|-------|
| `Source` | `unit`, `plugin-e2e-protocol` | Populated from `id` / `msg_id` and `ts`. |
| `Plain` | `unit`, `plugin-e2e-protocol` | Preserves text verbatim including spaces and multi-line content. |
| `At` | `unit`, `plugin-e2e-protocol` | Emitted when `is_at=true` and `bot_account_id` is known. |
| `AtAll` | `unit` | Emitted when `@所有人` is present in `content`. |
| `Image` | `unit` | Supports `<img_b64>...</img_b64>`, `img_b64`/`base64`, `http(s)` URL, and POSIX `path`; blocks remote Windows drive paths (`C:\...`) and falls back to `Plain("[图片]")`. |
| `Voice` | `unit` | Mapped for `type=34` with `voice_b64` / `url` / POSIX `path` and `length`. |
| `File` | `unit` | Mapped for `type=49` when `file_name` or `file_url` is present. |
| `Quote` | `unit` | Mapped when `quote` / `refer` dict is present. |
| `Face` | `not-supported` | Custom stickers (`type=47`) map to `Image(url=...)` when a CDN URL is present, otherwise `Unknown("[表情]")`. |
| `Forward` | `not-supported` | Merged-forward XML parsing is not implemented in the gateway payload. |
| `Unknown` | `unit` | Unsupported types without text become `Unknown` without crashing. |

### Outbound (`yiri2target` + `send_message` / `reply_message`)

| Component | Evidence Level | Notes |
|-----------|----------------|-------|
| `Plain` | `unit`, `plugin-e2e-protocol` | Sent via `POST /api/send` (`receiver`, `msg`, `aters`). |
| `At` / `AtAll` | `unit`, `plugin-e2e-protocol` | Coalesced into `aters` (`wxid` or `notify@all`). |
| `Image` | `unit`, `plugin-e2e-protocol` | Sent via `POST /api/send_image` (`url`, `base64`, or `path`). |
| `File` | `unit`, `plugin-e2e-protocol` | Sent via `POST /api/send_file` (`url`, `base64`, `path`, `filename`). |
| `Voice` | `unit`, `plugin-e2e-protocol` | Sent via `POST /api/send_voice` (`url`, `base64`, `path`, `filename`). |
| `Quote` / `Face` / `Forward` | `unit` | Rendered into readable text fallback; native WeChat quote/forward APIs are not exposed by the gateway. |
| Mixed chain | `unit`, `plugin-e2e-protocol` | Adjacent text/mentions coalesce while media operations preserve exact chain order. |

## Common APIs

| API | Declaration | Evidence Level | Notes |
|-----|-------------|----------------|-------|
| `send_message` | Required | `unit`, `plugin-e2e-protocol`; `blocked` (`plugin-e2e-outbound`) | Returns `MessageResult(message_id=..., raw=...)`. |
| `reply_message` | Required | `unit`, `plugin-e2e-protocol`; `blocked` (`plugin-e2e-outbound`) | Accepts both EBA `MessageReceivedEvent` and legacy `FriendMessage`/`GroupMessage`. |
| `get_user_info` | Optional | `unit`, `plugin-e2e-protocol` | Calls `GET /api/user_name?wxid=<id>`. |
| `get_friend_list` | Optional | `unit`, `plugin-e2e-protocol` | Calls `GET /api/contacts` and filters non-`@chatroom`, non-`gh_*` contacts. |
| `get_group_list` | Optional | `unit`, `plugin-e2e-protocol` | Calls `GET /api/contacts` and filters `@chatroom` entries. |
| `get_group_info` | Optional | `unit`, `plugin-e2e-protocol` | Resolves group from `get_group_list()`; raises `NotSupportedError` when missing. |
| `get_group_member_list` | Optional | `unit`, `plugin-e2e-protocol` | Calls `GET /api/chatroom_members?roomid=<id>`. |
| `call_platform_api` | Optional | `unit`, `plugin-e2e-protocol`, `adapter-live` (`get_status`) | Dispatches actions in `PLATFORM_API_MAP`; unknown actions raise `NotSupportedError`. |
| `edit_message`, `delete_message`, `forward_message`, `leave_group`, `upload_file`, `get_file_url`, `kick_group_member`, `mute_group_member`, `unmute_group_member` | Unsupported | `not-supported` | Explicitly raise `NotSupportedError`. |

## Platform-Specific APIs (`call_platform_api`)

| Action | Endpoint | Evidence Level | Notes |
|--------|----------|----------------|-------|
| `get_status` | `GET /api/status` | `unit`, `adapter-live` | Verified against live `WeChat 4.1 Native Gateway (v4)` (`wechat_version=4.1.15.56`, `engine=v4`). |
| `get_friend_requests` | `GET /api/friend_requests` | `unit`, `plugin-e2e-protocol` | Returns 503 when the WeChat PC client is logged out. |
| `accept_friend` | `POST /api/accept_friend` | `unit`, `plugin-e2e-protocol` | Mutating action; requires `request_id`. |
| `send_rich_text` | `POST /api/send_rich_text` | `unit`, `plugin-e2e-protocol` | Sends WeChat link card (`receiver`, `title`, `url`, optional `name`/`account`/`digest`/`thumburl`). |

## Acceptance Verification Log (2026-11-22)

- **Unit & Protocol Loopback (`tests/unit_tests/platform/test_wechatferry_eba_adapter.py`)**: All unit tests and the real `aiohttp` + `websockets` loopback test passed (manifest parity, deduplicator, inbound/outbound converters, mixed chain ordering, EBA + legacy dispatch, common APIs, `NotSupportedError` guards, platform APIs).
- **Live Gateway Endpoint Probe (`adapter-live`)**: Probed live `WeChat 4.1 Native Gateway (v4)` on `2026-11-22`:
  - `GET /openapi.json` returned `200 OK` confirming all 11 mapped endpoints and request body schemas.
  - `GET /api/status` returned `200 OK` with `wechat_version="4.1.15.56"`, `engine="v4"`, `is_login=false`, `ws_clients=1`.
- **Blocked Items (`plugin-e2e-ui` / `plugin-e2e-outbound`)**:
  - Live WeChat PC client on the test host was logged out (`is_login=false`) during verification, so `GET /api/contacts` and `GET /api/friend_requests` returned `503 Service Unavailable`, and real human-UI inbound/outbound messaging (`plugin-e2e-ui`, `plugin-e2e-outbound`) is recorded as **`blocked`** rather than assumed passed.
  - Once WeChat PC is logged in (`is_login=true`), run the live probe and standalone runtime E2E sweep to upgrade the remaining items to `plugin-e2e-ui` / `plugin-e2e-outbound`:
    ```bash
    WCF_API_URL=http://127.0.0.1:18080 \
    WCF_WS_URL=ws://127.0.0.1:18080/ws \
    WCF_SEND_TARGET=<test_wxid_or_chatroom> \
    uv run python tests/e2e/live_wechatferry_eba_probe.py --wait-seconds 30
    ```
