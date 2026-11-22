# The stream test workshop

Fake OBS, fake Twitch chat, recorded event shapes and the contract tests that hold Nox's stream
code to them. Everything runs on loopback, headless, with no OBS, no Twitch account and no network
beyond `127.0.0.1`. Nothing under `src/nox` imports from here: this is test support only.

## What is in it

| Piece | Where | What it is |
|---|---|---|
| Fake OBS | `fake_obs/` | An obs-websocket v5 server with a small stateful OBS behind it |
| Fake Twitch chat | `fake_twitch.py` | The IRC side of Twitch chat, with Twitch's send rules |
| Recordings | `recordings/*.ndjson`, `recordings.py` | Event shapes of a chat evening and an OBS session, invented content |
| Raw clients | `raw_clients.py` | Protocol clients that share no code with Nox, for testing the fakes |
| Contract tests | `tests/unit/workshop/` | Nox's clients and plugins against the fakes |

The fakes do not import from `nox` on purpose. The contract tests compare Nox's client with an
implementation of the protocol, and a fake built from the client's own constants (or its own
authentication hash) would agree with it by construction. `test_fake_obs.py` and
`test_fake_twitch.py` check each fake against the specification with the raw clients, so a wrong
fake shows up there and not as a confusing failure in a Nox test.

## Fake OBS (`fake_obs`)

```python
async with FakeObs(password="correct-horse", scenes=("Start", "Live")) as obs:
    client = ObsWebSocketClient(obs.url, password_provider=..., on_event=...)
    ...
    assert obs.state.current_scene == "Live"  # what OBS ended up as
    assert obs.requests_of("SetCurrentProgramScene") == [{"sceneName": "Live"}]
```

- **Protocol:** Hello with challenge and salt, Identify with the authentication string, Identified,
  Request / RequestResponse, Event filtered by the event subscriptions the client identified with.
  Close codes 4007 (request before Identify), 4008 (second Identify), 4009 (wrong password), 4010
  (RPC version too new). Not implemented: request batches, Reidentify.
- **State:** scenes and their items, inputs with settings and mute, filters per source, stream /
  record / replay-buffer outputs, audio devices and tracks. Requests change it and emit the events
  real OBS emits (`CurrentProgramSceneChanged`, `InputCreated`, `StreamStateChanged` with STARTING
  and STARTED, `ReplayBufferSaved`, ...). Read it through `obs.state`.
- **Requests answered:** `GetVersion`, `GetStats`, scenes (`GetSceneList`, `GetCurrentProgramScene`,
  `SetCurrentProgramScene`, `CreateScene`, `RemoveScene`), scene items (`GetSceneItemList`,
  `GetSceneItemId`, `CreateSceneItem`, `SetSceneItemEnabled`), inputs (`GetInputList`,
  `CreateInput`, `RemoveInput`, `GetInputSettings`, `SetInputSettings`, `GetInputMute`,
  `SetInputMute`, `GetInputPropertiesListPropertyItems`, `SetInputAudioTracks`), filters
  (`GetSourceFilterList`, `CreateSourceFilter`, `SetSourceFilterEnabled`, `RemoveSourceFilter`),
  outputs (`GetStreamStatus`, `StartStream`, `StopStream`, `GetRecordStatus`, `StartRecord`,
  `StopRecord`, `GetReplayBufferStatus`, `SaveReplayBuffer`). Anything else is `UnknownRequestType`
  (204); a missing or mistyped field, an unknown scene or input, a duplicate name, a bad input or
  filter kind and a stopped output answer with the documented status codes.
- **Answers are modelled on real OBS, not byte-exact:** comments are close to OBS's, optional
  fields a client does not read may be missing. Add what a test needs the day it needs it, as a
  `req_<snake_case_request>` method in `fake_obs/requests.py` that returns the response data and
  appends the events it causes.
- **Failures, scripted and never random:** `fail_next(type, code, comment, times=)` answers with a
  failure; `ignore(type)` never answers (the client's timeout ends it); `hold(type)` answers when
  the returned event is set; `drop_on(type)` cuts the connection when that request arrives;
  `drop_connections()` cuts every connection like a pulled cable; `close_connections(code)` closes
  them properly; `refuse_connections(n)` closes the next `n` before Hello; `stall_hello()` accepts
  and says nothing; `script(type, fn)` replaces an answer; `emit(type, data)` sends an event by
  hand; `replay_events(...)` sends recorded ones.
- **Watching:** `requests`, `requests_of(type)`, `identifies` (RPC version, subscriptions, whether
  it authenticated), `connection_count`, `arrived(type)` (an event set when a request came in).

## Fake Twitch chat (`fake_twitch.py`)

```python
async with FakeTwitchIrc() as irc:
    ...  # the plugin connects to irc.port
    await irc.privmsg("viewer_01", "hallo", subscriber=True, bits=50)
    await irc.usernotice("raid", "raider_01", params={"viewerCount": "24"})
    assert [d.text for d in irc.delivered] == ["..."]  # what Twitch accepted from the bot
```

- **Protocol:** `PASS` / `NICK` answered by the welcome numerics, or by `Login authentication
  failed` / `Improperly formatted auth` and a closed connection; `CAP REQ` answered by ACK or NAK;
  `JOIN` answered by the echoed join, the names list, `ROOMSTATE` and `USERSTATE`; `PING` / `PONG`;
  `PRIVMSG` judged by Twitch's send rules: at most `send_limit` messages per `send_window_s`
  (`msg_ratelimit` notice when over) and no identical message twice in a row (`msg_duplicate`).
- **Time** for those rules is the injected `clock` (a callable); move time by changing what it
  returns. Message ids and timestamps count up from fixed starts and user ids come from the login,
  so a run looks the same every time.
- **Scripting from the server:** `privmsg(...)` with the full tag set (badges, subscriber, mod,
  bits, emotes, reply parent, first message, action), `usernotice(msg_id, login, params=)`,
  `notice(...)`, `ping()`, `send_raw(line)`, `send_lines([...])`, `reconnect()` (`RECONNECT`, then
  close).
- **Failures:** `refuse_connections(n)`, `go_silent()` (a connection that died without a FIN),
  `drop_connections()`.
- **Watching:** `connections` (each with its `lines`, `nick`, `capabilities`, `channels`),
  `lines_of("JOIN")`, `delivered`, `refused_for_rate`, `refused_as_duplicate`, `pongs`.

## Recordings (`recordings/`, `recordings.py`)

A recording is a newline-delimited JSON file (`.ndjson`): a `note`, then `server` entries, each with a `seq`. A Twitch
entry holds one IRC `line`, an OBS entry one `event` (`eventType`, `eventData`). Replay sends the
`server` entries in order, as fast as the receiver reads them: `irc.send_lines(irc_lines(name))`,
`obs.replay_events(obs_events(name))`. A recording has no timing, so a replay never depends on how
long anything takes.

The two recordings follow the shapes the services document and send. **Every name, id, channel,
file and text in them is invented** (`viewer_01`, `examplechannel`, `example.invalid`);
`check_recording` holds each file to that and `test_recordings.py` runs it over the folder, so a
real login, a link, an address or a credential fails the test. To add a recording, write the file,
list what it must produce in a replay test, and keep the content made up.

| File | Holds |
|---|---|
| `twitch_chat_evening.ndjson` | `ROOMSTATE`, plain, emote, bits, reply, action and umlaut chat lines, another bot's command, sub, resub, a mass gift with its single gifts, a raid, a ritual notice, `CLEARCHAT`, `CLEARMSG`, a slow-mode notice, `PING` |
| `obs_stream_session.ndjson` | scene changes, mute, going live (STARTING, STARTED), recording, going offline (STOPPING, STOPPED), `ExitStarted` |

## The contract tests (`tests/unit/workshop/`)

- `test_obs_contract.py` - `nox.stream.obs_ws.ObsWebSocketClient` and the OBS plugin: login
  (rpc version, subscriptions, the authentication hash), a wrong or missing password reported once,
  recovery when the password is fixed, request / response matching, refusals with OBS's code and
  comment, a request that is never answered, a connection cut in the middle of a request, events
  after a reconnect, the wait between attempts growing to its ceiling, a clean close, stopping while
  OBS never says hello; and through the plugin: scene switch, the scene-set allow list, sources
  made through `obs.request`, the privacy scene and the hard prohibitions refused before OBS sees
  them, stream and recording events, the replay buffer, overlay and voice track setup, panic, health
  while OBS is lost and when it returns.
- `test_twitch_contract.py` - `nox_plugin_twitch.irc_client.TwitchIrcClient` and the Twitch plugin:
  the login lines in order, the join echo, `PING`, tags decoded, notices, lines the client has no
  use for, a refused login, missing credentials, the egress check, refused connections and the
  backoff, `RECONNECT`, a connection cut or gone quiet, stopping; and through the plugin: chat as
  events, a command answered in chat, replies to the bot, funken requests for subs, gifts and
  raids, the plugin's own send budget against Twitch's, a refusal from Twitch, a duplicate, an
  outage with nothing queued, health reasons.
- `test_recordings.py` - the recordings are fit to be public; a replay gives exactly the events they
  stand for.
- `test_fake_obs.py`, `test_fake_twitch.py` - the fakes against the specifications.

The older fakes in `tests/unit/plugins/obs/fake_obs_server.py` and
`tests/unit/plugins/twitch/fake_irc_server.py` stay for the tests that already use them. New tests
that need more than a canned answer should use the workshop.

## Rules for tests that use it

- Loopback only, ports from the OS (`127.0.0.1`, port 0); nothing is started outside a test, and
  `async with` / the fixtures stop every server and connection.
- No sleeping for a fixed time: wait for a condition with `tests.unit.fakes.wait_until`, or send a
  marker the system must handle after the thing that must not happen and wait for the marker.
- Time that a rule depends on is the fake's injected `clock`, not the wall clock.
- Only invented content: `viewer_01`, `examplechannel`, `example.invalid`, no real accounts, no
  real tokens (a password in a test is a literal like `correct-horse`).
- Add a fake behaviour together with the test that pins it in `test_fake_*.py`.
