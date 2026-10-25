# DeepSeek Web API — standalone Python

`deepseek_web_api.py` turns your [chat.deepseek.com](https://chat.deepseek.com) web account into
an OpenAI-compatible API on your own machine. It is a single-file Python port of OmniRoute's
`deepseek-web` provider (`open-sse/executors/deepseek-web.ts` and its helpers): no OmniRoute, no
Node.js, no `pip install`. You sign in once in a Chromium-based browser window and the script
keeps the token.

- `POST /v1/chat/completions`: streaming and non-streaming, DeepThink reasoning
  (`reasoning_content`), web search with a source list, tool calling, image input
- `GET /v1/models`
- Requirements: Python 3.9+ and Chromium, Google Chrome, Microsoft Edge or Brave

## Quick start (Windows)

1. Install Python 3.9 or newer from [python.org](https://www.python.org/downloads/) (tick
   **Add python.exe to PATH**).
2. Put `deepseek_web_api.py` and `start.bat` in a folder.
3. Double-click `start.bat`. On the first run a browser window opens on chat.deepseek.com: sign
   in (Google, email or phone). The window closes by itself and the server starts:

   ```text
   DeepSeek Web API is running
     Base URL : http://127.0.0.1:20129/v1
   ```

The same from a terminal: `py deepseek_web_api.py login`, then `py deepseek_web_api.py`. On macOS
and Linux use `python3 deepseek_web_api.py`.

## Use it from any OpenAI client

Base URL `http://127.0.0.1:20129/v1`, any API key (unless you start the server with `--api-key`),
and one of the model names below.

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:20129/v1", api_key="anything")
reply = client.chat.completions.create(
    model="deepseek-v4-flash-think",
    messages=[{"role": "user", "content": "Is 391 prime?"}],
)
print(reply.choices[0].message.content)
```

```bash
curl http://127.0.0.1:20129/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model": "deepseek-v4-flash", "stream": true, "messages": [{"role": "user", "content": "Hello"}]}'
```

## Models

| Model                                                         | DeepSeek web mode                |
| ------------------------------------------------------------- | -------------------------------- |
| `deepseek-v4-flash`, `deepseek-chat`                          | Instant                          |
| `deepseek-v4-flash-think`, `DeepSeek-R1`, `deepseek-reasoner` | Instant + DeepThink              |
| `deepseek-v4-flash-search`, `DeepSeek-Search`                 | Instant + web search             |
| `deepseek-v4-flash-think-search`, `DeepSeek-R1-Search`        | DeepThink + web search           |
| `deepseek-v4-pro` (and `-think`, `-search`, `-think-search`)  | Expert, when your account has it |

Any other name works too: `think`, `r1` or `reason` in the name turns on DeepThink, `search` turns
on web search, `pro` or `expert` asks for the Expert model. DeepSeek decides which model types an
account gets; when Expert isn't enabled the script uses Instant and logs a warning
(`py deepseek_web_api.py status` lists your enabled types). Request flags work as well:
`reasoning_effort`, `thinking_enabled`, `search_enabled`, `web_search`, `web_search_options`.
Sampling settings such as `temperature` and `max_tokens` are ignored, because the web app has none.

## Commands

| Command                                | What it does                                                       |
| -------------------------------------- | ------------------------------------------------------------------ |
| `py deepseek_web_api.py`               | Same as `serve`; runs `login` first if you aren't signed in        |
| `serve [--port 20129] [--api-key KEY]` | Runs the API server                                                |
| `login [--browser edge]`               | Opens a browser window to sign in and saves the token              |
| `login --manual`                       | Paste a `userToken` (or a DevTools "Copy as cURL") instead         |
| `status`                               | Shows the saved login, checks it, lists enabled model types        |
| `chat "Hello" [--think] [--search]`    | Sends one prompt from the terminal                                 |
| `logout [--all]`                       | Deletes the saved token (`--all` also deletes the browser profile) |

Useful `serve` options: `--host 0.0.0.0` (other devices; use `--api-key` too), `--cors` (browser
apps on other origins), `--keep-sessions` (keep the temporary chats in your DeepSeek history),
`--history-window N`, `--proxy URL`, `--verbose`. Run `--help` on any command for the full list.

Environment variables: `DEEPSEEK_WEB_API_HOME` (data folder), `DEEPSEEK_USER_TOKEN` (use this token
instead of the saved login), `DEEPSEEK_WEB_API_KEY` (default for `--api-key`).

## How the login works

The script starts the browser as a normal program with its own profile
(`%LOCALAPPDATA%\DeepSeekWebAPI\browser-profile` on Windows) and the DevTools port enabled. It
doesn't use automation flags, and it never connects to a tab until that tab is on
chat.deepseek.com. That matters for "Sign in with Google", which refuses browsers driven by
automation tools. Once the page holds a `userToken`, the script saves the token, the
chat.deepseek.com cookies, the browser's user agent and the web app's client version to
`credentials.json` next to the profile, then closes the browser. The profile is kept, so signing in
again is usually instant. If DeepSeek rejects the saved token, the server re-reads it from the
profile in a hidden browser once and retries.

Browsers are tried in this order: Chromium, Google Chrome, Microsoft Edge, Brave, then a Chromium
installed by Playwright (`pip install playwright` then `python -m playwright install chromium`).
Use `--browser chrome|edge|brave|chromium` or `--browser "C:\path\to\browser.exe"` to choose.

## What happens on each request

1. The saved `userToken` is exchanged for a bearer token (`/api/v0/users/current`, cached for an
   hour).
2. A temporary chat is created and DeepSeek's proof-of-work challenge is solved. That is
   `DeepSeekHashV1`, SHA3-256 with 23 Keccak rounds. Pure Python solves it in about half a second
   on average (under a second worst case) by hashing 1024 candidates at once.
3. The messages become one prompt, since the web endpoint takes text rather than a message list.
   Multi-turn chats replay the last 20 turns and tool results. With `tools`, the OpenAI tool list
   is turned into a prompt contract and the reply is parsed back into `tool_calls`. That covers the
   many formats DeepSeek uses, with a per-request nonce and schema check so stray JSON is never run
   as a tool call.
4. The answer streams back in OpenAI format and the temporary chat is deleted.

## Differences from OmniRoute's TypeScript version

These were found while testing against the live site and are fixed here:

- Stream events can omit the operation field (`"o"`) when it hasn't changed. This script reads it
  the way the web app does. OmniRoute instead removes the word `FINISHED` from every answer to hide
  a status event it misreads.
- Search sources now arrive under `response/fragments/-1/results`. OmniRoute still reads the old
  `response/search_results` path, so its search answers lose their sources.
- The web client is at version 2.7.0. `login` reads the live version instead of sending a
  hardcoded 2.0.0.
- When a tool call sits inside a DSML wrapper, OmniRoute removes both with stale offsets and can
  delete text that follows. Overlapping ranges are merged here.

Extras: reasoning streams live during tool calls, answers DeepSeek cuts off are continued
automatically, approximate token usage is reported, the `developer` role counts as `system`,
`tool_choice` and `response_format` add instructions to the prompt, and images are uploaded the
way the web app does.

## Security and privacy

- `credentials.json` holds a token that gives full access to your DeepSeek account. Keep it
  private; `logout` deletes it.
- The server listens on `127.0.0.1` only, rejects requests whose `Host` isn't local (DNS-rebinding
  protection) and sends no CORS headers unless you pass `--cors`. Use `--api-key` whenever you
  open it up beyond your machine.
- This uses DeepSeek's private web API, which can change without notice. Use it within DeepSeek's
  terms of service.

## Troubleshooting

| Problem                                                | Fix                                                                     |
| ------------------------------------------------------ | ----------------------------------------------------------------------- |
| "No Chromium-based browser found"                      | Install Chrome or Edge, pass `--browser PATH`, or use `login --manual`  |
| Google says the browser "may not be secure"            | Try `login --browser chrome` or `--browser edge`, or sign in with email |
| "The DeepSeek login profile is open in another window" | Close the browser window the script opened earlier                      |
| `401` / "DeepSeek rejected the login token"            | Run `login` again                                                       |
| `503` "DeepSeek's firewall blocked the request"        | Run `login` again to refresh the cookies, or wait a few minutes         |
| "Could not listen on 127.0.0.1:20129"                  | Another program uses the port; start with `--port 20130`                |
| Behind a corporate proxy                               | The Windows proxy is used automatically; otherwise pass `--proxy URL`   |

## Tests

From the repository root (`py` instead of `python` on Windows):

```bash
python -m unittest discover -s tests/python -v
```

The suite runs the real server against a local fake of chat.deepseek.com, so it needs no network
or account. Set `DEEPSEEK_TEST_BROWSER=1` to include a headless-browser login test.
