# UCAS Course Sign-in

A small command-line tool for **searching UCAS courses, signing in directly, or
showing a scannable sign-in QR code**.

It runs entirely in the terminal with plain prompts — no full-screen UI, no
browser. You answer a few questions (mail, password) and then pick a course by
number; the tool can either call the upstream sign-in endpoint for you or
render a QR code that you scan with the UCAS mobile app.

> This is a Python rewrite of [lccipher/UCAS-Course-Sign-in](https://github.com/lccipher/UCAS-Course-Sign-in).
> The original is a Next.js web app that has to be deployed to Vercel, and
> `vercel.app` is not always reachable from mainland China. This version runs
> locally — **no deployment, no browser**.

>[!CAUTION]
> **This project is for learning and personal use only. Do not use it for any commercial or illegal purpose.**

## Features

- **Prompt-based login**: you are asked for your mail and password once, in
  the spirit of FreeBSD's `adduser`. The password is read without echo.
- **No date to enter**: the date is always *today*, taken from the calibrated
  UCAS server clock.
- **Course list**: today's schedule is fetched automatically and shown in a table.
- **Direct sign-in**: pick a course by its number and the tool signs in for it.
- **Sign-in QR code**: `qr <number>` renders a scannable QR code in the terminal
  and prints a fresh one every 5 seconds, so the embedded timestamp stays valid.
- **Sign-in window hints**: `Open` / `Not open` / `Closed` is shown for every course.
- **Server clock calibration**: syncs with the UCAS timestamp server and
  compensates for the ~3s drift between its two servers.
- Refreshes the course status automatically after a successful sign-in.
- **Automatic sign-in**: a class-calendar scheduler (`server`) that signs in for
  you at every class-period start and pushes the result to your phone. It is
  meant to run in the shipped
  `Dockerfile` / `docker-compose.yml` stack (see
  [Automatic sign-in](#automatic-sign-in-docker)).

All UCAS requests go straight to `iclass.ucas.edu.cn:8181`, and credentials are
only held in memory for the current session — never written to disk. When
notifications are enabled, only the sign-in *result* (course names and status,
no credentials) is sent to your Feishu group, which may be a third-party
server.

## Requirements

- Python **3.10+**
- [uv](https://docs.astral.sh/uv/) (or `pipx`) for installation

## Install & run

### Run from a clone (uv native, recommended)

`uv` reads `pyproject.toml` and `uv.lock`, creates `.venv` on demand and runs
the project's console scripts inside it — no manual `venv` activation and no
separate install step:

```bash
uv sync                  # create .venv and install the project (editable)
uv run tui               # interactive course list / sign-in
uv run server            # auto sign-in scheduler (runs at every class-period start)
```

Pass a specific interpreter on the first sync if you like:
`uv sync --python=3.12`.

### Install as a command

To get `tui` / `server` on your `PATH` without keeping a checkout around, use
`uv tool` (or `pipx`), which installs the app into its own managed
environment:

```bash
uv tool install .
tui
# or
pipx install .
tui
```

## Usage

Start the program and answer the prompts:

```text
UCAS Course Sign-in
===================
Mail: you@mails.ucas.ac.cn
Password:
Signing in... done.
Fetching courses for 2026-03-25... done.

  #  Course              Teacher  Time           Status      Sign-in window
  -  ------------------  -------  -------------  ----------  --------------
  1  分布式系统          张三     10:25 ~ 11:15  Not signed  Open
  2  机器学习与数据挖掘  李四     13:30 ~ 15:20  Signed      Not open

Enter a course number to sign in, or one of:
  <number>     sign in for the course with that number
  qr <number>  show a scannable sign-in QR code for that course
  r            reload today's courses
  ?            show this help
  q            quit

Select: 1
Signing in for '分布式系统'... done.
  ✓ Sign-in successful (sign-in record 123456)
```

After a successful sign-in for a listed course, the schedule is reloaded so the
status column stays current.

### Commands

| Input | Action |
| --- | --- |
| `<number>` | Sign in for the course with that number |
| `qr <number>` | Show a scannable sign-in QR code for that course |
| `r` | Reload today's courses |
| `?` | Show the command help |
| `q` | Quit |

### QR code

`qr <number>` prints the upstream sign-in URL as a QR code, exactly like the
web version: it encodes `stu_scan_sign.action` with the course ID and a
calibrated timestamp (no user id). Because the URL embeds a timestamp, a stale
code is rejected by the upstream, so a **fresh code is printed every 5 seconds**
and simply scrolls down; the newest one is the one to scan.

- press **`b`** to go back to the course list.

Showing a QR code does **not** sign in by itself: you scan it with the UCAS app
and confirm there.

## Automatic sign-in (Docker)

Besides the interactive TUI, the project ships a non-interactive scheduler
(`server`) that is meant to run in the deployed Docker stack. It signs in for
you **at every class-period start of the UCAS academic calendar** (08:25,
09:15, 10:20, 11:10, 13:25, 14:15, 15:20, 16:10, 17:00, 18:25, 19:15, 20:10,
21:00 Beijing time, hardcoded in `src/server/server.py`) and only notifies you
when there is something to say.

What each run does:

| Situation | Action | Log level |
| --- | --- | --- |
| No courses today | silent | INFO |
| A course is already signed in (upstream) | silent | INFO |
| Courses today, but none is in its sign-in window | silent | INFO |
| A course is in its sign-in window and not yet signed, sign in succeeds | push | WARNING |
| A course is in its sign-in window and not yet signed, sign in fails | push | CRITICAL |
| Today's date or the course list cannot be fetched | push | CRITICAL |
| The upstream answers with a shape nobody implemented (server crashes on purpose) | push + exit | CRITICAL |
| Configuration error (server exits) | log only | CRITICAL |
| Notification configuration error (server exits) | log only | CRITICAL |

"In its sign-in window" means from 30 minutes before the class starts until the
class ends -- the same window the upstream enforces. A pass runs at each
class-period start, which always lands inside the window of a class beginning
then; when a class spans several periods, later passes are retries.

Each run is idempotent per day: every pass re-fetches today's course list and
trusts the sign-in flag **UCAS itself returns**, so a course that is already
signed in is never signed or pushed twice. Nothing is persisted locally -- no
state file, no volume. Failures are retried on the next class-period run.

> [!IMPORTANT]
> The course date and the sign-in window are always evaluated in
> `Asia/Shanghai` (China Standard Time, UTC+8). The timezone is **hardcoded**
> in the image and in the code -- there is no `TZ` variable to set, and the
> container's own timezone cannot shift the window.

### Quick start with Feishu

Feishu / Lark (飞书) works with a **personal account**: create a group, open
`Settings -> Group bots -> Add bot -> Custom bot`, pick a security mode and copy
the webhook. Keep the group to yourself only if you don't want anyone else to
receive the notifications.

1. Choose a security mode:
   - **Signature verification**: copy the secret and set `UCAS_FEISHU_SECRET`.
2. Copy the webhook (`https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx`).
3. Put it in `.env`:

```bash
UCAS_FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
UCAS_FEISHU_SECRET=xxxxxxxxxxxxxxxx   # only for "signature verification"
```

Then `docker compose up -d --build` and watch the runs with
`docker compose logs -f`.

### Deploy with docker compose

```bash
cp .env.example .env
$EDITOR .env                 # credentials + Feishu webhook

docker compose up -d --build
docker compose logs -f        # watch the runs
```

Useful commands:

```bash
docker compose ps
docker compose restart
docker compose down

# show the scheduler's help / environment reference without starting it
# (the image entrypoint is `python -m server`)
docker compose run --rm ucas-course-sign-in -h
```

There is no separate one-shot command: the container always runs the
class-calendar scheduler, and with `UCAS_RUN_ON_START=1` (the default) it also
performs one pass immediately on start.

The server logs through the Python standard `logging` module to stderr, one line
per event, timestamped in `Asia/Shanghai`: `docker compose logs -f` is all you
need to follow it. Notifications are log-driven too (see below).

### Notifications (Feishu)

Feishu / Lark (飞书) is the only notification provider. The bot posts a `text`
message to the group that owns the webhook, so keep that group to yourself only
if the messages should stay private.

Notifications are **log-driven**: a `logging` handler pushes every record at
`WARNING` or above from the server's own loggers. So a successful run (logged at
`INFO`) stays silent, while a sign-in failure or a failed fetch (`CRITICAL`) is
pushed once. Add `extra={"title": ...}` to a log call to control
the message title. Library noise (`httpx`, ...) and anything below `WARNING` are
never pushed.

| Variable | Default | Description |
| --- | --- | --- |
| `UCAS_FEISHU_WEBHOOK` | -- | Full custom-bot webhook URL (**required**) |
| `UCAS_FEISHU_SECRET` | -- | Secret for the "signature" (签名校验) security mode |

The webhook already contains the bot token. With the "signature" mode, set
`UCAS_FEISHU_SECRET`; `timestamp` and `sign` are added to the JSON body. With the
"custom keyword" mode, make sure the keyword appears in the messages -- the
notifications' titles already contain `UCAS`. Messages are sent as Feishu `text`.
If `UCAS_FEISHU_WEBHOOK` is missing, the server exits with a configuration error
instead of silently dropping notifications; a failed push is likewise never
swallowed -- if the webhook stops working, the process crashes on purpose
instead of letting sign-in results vanish silently.

### Error handling

Every deliberate error derives from `UcasError` (`src/common/error.py`), and the
class hierarchy encodes what may happen to it:

* `UcasOperationalError` -- bad input (`UcasTimeError`, `UcasUnrecognizableCourse`),
  rejected credentials (`UcasAuthError`), network trouble (`UcasNetworkError`) and
  upstream trouble (`UcasServerError`, `UcasJsonError`). These never crash the
  server: each pass catches them, logs at `CRITICAL` (which pushes) and carries on;
  the next class-period run retries.
* `UcasNotImplementedError` derives from `UcasError` only. It marks an upstream
  response shape nobody has written handling for, and crashing the process is the
  intended behaviour: nobody catches it, the fatal log pushes one last message,
  and the traceback lands in `docker compose logs` so the missing path gets noticed
  and implemented.

So `except UcasOperationalError` is always safe, while `except UcasError` would
swallow the deliberate crashes.

### Auto-sign environment variables

Notification settings are listed under [Notifications (Feishu)](#notifications-feishu);
the remaining variables are:

| Variable | Default | Description |
| --- | --- | --- |
| `UCAS_USERNAME` | -- | UCAS mail (required) |
| `UCAS_PASSWORD` | -- | Password (from `.env`) |
| `UCAS_RUN_ON_START` | `1` | Run a pass immediately on server start |

The schedule endpoint reports three states in its `STATUS` field, and the server
now tells them apart: `0` = ok, `1` = a real error (surfaced and pushed), `2` =
no courses that day (quiet).

### How the schedule works

The server wakes at every class-period start (`08:25`, `09:15`, ... Beijing
time, hardcoded from the academic calendar) and runs a sign-in pass.
No cron or extra scheduler dependency is involved. Shutdown is a regular
server-style `KeyboardInterrupt`: `SIGTERM`/`SIGINT` (as sent by `docker stop`)
are routed to it, so both interrupt the sleep and exit gracefully.

## How it works

```text
CLI
 ├─ POST login.action                     → get sessionId / userId
 ├─ GET  get_stu_course_sched.action      → course list for the day
 ├─ POST get_timestamp.do                 → calibrate the server clock offset
 └─ GET  stu_scan_sign.action             → direct sign-in (or QR payload)
        courseSchedId=<7-digit course ID> or timeTableId=<UUID>&timestamp=<calibrated>&id=<userId>
```

Details preserved from the original port:

- **Timestamp buffer**: `get_timestamp.do` and `stu_scan_sign.action` run on
  different servers whose clocks drift by ~3 seconds, so the sign-in timestamp
  is the calibrated server time minus 3 seconds.
- **Sign-in window**: from 30 minutes before class until the class ends. The tool
  shows the window state but does not block sign-in — the upstream response is
  the source of truth.
- The web app's server-side protections (same-origin check, rate limiting) were
  **removed**, because requests originate from the local machine and no public
  service is exposed.

## Project layout

```text
.
├─ pyproject.toml
├─ Dockerfile                 # deployable image (class-calendar server by default)
├─ docker-compose.yml         # server deployment
├─ .env.example               # copy to .env and fill in
├─ src/
│  ├─ common/                 # shared logic, used by both front ends
│  │  ├─ __init__.py          #   version
│  │  ├─ api.py               #   upstream client + pure logic (login / schedule / sign-in / clock)
│  │  └─ error.py             #   UcasError hierarchy
│  ├─ tui/                    # interactive front end
│  │  ├─ __main__.py          #   python -m tui
│  │  └─ tui.py               #   prompt flow and QR code
│  └─ server/                 # automatic front end
│     ├─ __main__.py          #   python -m server
│     ├─ autosign.py          #   one-pass sign-in + notification
│     ├─ server.py            #   class-calendar scheduler
│     ├─ logging.py           #   stdlib logging + notify handler (stderr/Push)
│     └─ notify.py            #   Feishu notifications
```

## Development

```bash
uv venv --python=3.12
source .venv/bin/activate
uv pip install -e ".[dev]"

# Run the TUI locally
tui

# Lint
ruff check .
```

`UcasClient` accepts an injected `httpx.Client`, which makes it easy to point
at a different upstream or to inject a `MockTransport` in tests. `tui.tui.main()`
accepts an injected `client=...` for the same reason.

## Differences from the web version

| | Web version | This tool |
| --- | --- | --- |
| Deployment | Requires Vercel / a server | Runs locally, no deployment |
| Reachability | Depends on `vercel.app` | Only depends on the UCAS upstream |
| Browser | Required | Not required |
| Interface | Web page / QR code | Terminal prompts |
| Sign-in method | Generate a QR → scan with phone, or direct sign-in | Direct sign-in, or a terminal QR code to scan |
| QR codes | Generate / refresh / download as PNG | Rendered in the terminal and refreshed on demand |
| Server-side protections | Same-origin check / rate limiting | Not needed (direct local requests) |
| Credentials | Sent to the deployed server | Kept in local memory only |

## Disclaimer

- This project is for learning and personal use only. Do not use it commercially or illegally.
- You are solely responsible for any consequences of using this project.
- The upstream API may change at any time; if it stops working, open an issue or adapt it yourself.

## License

[AGPL-3.0](./LICENSE) (same as the original project)
