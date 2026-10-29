"""The open-weight model the text-only workers use (owner, 2026-09-28: "Lets do 1").

The reply AI, outreach writer and send judge, follow-ups, the posting judge, the
recovery decisions and the answering AI all build a `codex exec` command for the ChatGPT
subscription. When `config["text_model"]` names a model, `command` rewrites that command to the
configured OpenRouter model and `environment` supplies its key; every other setting of the
worker's command is left exactly as it was. Without `text_model` nothing changes, so browser
work and research (which need OpenAI's browser tool and live web search) stay on GPT.

config.json:
    "text_model": {"model": "z-ai/glm-5.3"}
The key is the saved OpenRouter key (`credentials.get`: the OS store, E2.T4, or `openrouter.key`
in the user's Sidedoor home while one is left), or `key_file` when set.

`complete` (owner, 2026-10-13) is the direct alternative: one OpenRouter chat-completions call
with a chosen reasoning effort and a bounded max_tokens. Through codex the reasoning effort did
not reach the model, and codex asked for up to 131072 tokens, which OpenRouter refuses with 402
once credits run low. Every text worker uses it (2026-10-27: the reply, follow-up and outreach
workers too, through reply_worker.execute_text); `command` remains for the research recovery run.
"""

import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

import model_calls
import user_profile


KEY_VARIABLE = "OPENROUTER_API_KEY"
BASE_URL = "https://openrouter.ai/api/v1"
SUBSCRIPTION_SETTINGS = {'forced_login_method="chatgpt"', 'model_provider="openai"'}


def settings(config):
    value = (config or {}).get("text_model")
    return value if isinstance(value, dict) and isinstance(value.get("model"), str) and value["model"] else None


def model(config):
    """The model a text worker actually runs, for its recorded provenance."""
    chosen = settings(config)
    return chosen["model"] if chosen else (config or {}).get("model")


def auth(config):
    return "openrouter_api_key" if settings(config) else "existing_chatgpt_subscription"


def command(config, arguments):
    """The worker's own codex command, pointed at the open-weight model when one is configured."""
    chosen = settings(config)
    if not chosen:
        return arguments
    result = []
    for argument in arguments:
        if argument in SUBSCRIPTION_SETTINGS:
            result.pop()  # the "-c" in front of it
            continue
        result.append(argument)
    result[result.index("--model") + 1] = chosen["model"]
    start = result.index("exec") + 1
    result[start:start] = [
        "-c", 'model_providers.openrouter={name="OpenRouter",base_url="%s",env_key="%s",wire_api="responses"}'
        % (chosen.get("base_url") or BASE_URL, KEY_VARIABLE),
        "-c", 'model_provider="openrouter"']
    return result


def key(config):
    """The OpenRouter key: `key_file` when set (owner/dev), else the saved key (credentials.get:
    the OS store, or openrouter.key while one is left). Raises OSError when there is none, or
    credentials.Locked while the store is locked."""
    chosen = settings(config)
    configured = chosen.get("key_file")
    if configured:
        return Path(os.path.expanduser(configured)).read_text(encoding="utf-8").strip()
    import credentials
    value = credentials.get("openrouter", user_profile.home())
    if not value:
        raise FileNotFoundError("No OpenRouter key is saved")
    return value


def environment(config, variables):
    """`variables` plus the OpenRouter key when a text model is configured. Raises OSError if unreadable."""
    if not settings(config):
        return variables
    return dict(variables, **{KEY_VARIABLE: key(config)})


# --- Direct chat-completions call (owner, 2026-10-13: "Yes" to low reasoning for the yes/no judges) ---

ERROR_BODY_LIMIT = 300
CONTENT_LIMIT = 32768


class TextModelError(Exception):
    """A direct call that produced no usable answer. `record()` is safe to save: never the key."""

    status = None

    def record(self):
        return type(self).__name__ + ": " + str(self)


class TextModelHTTPError(TextModelError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__("HTTP %s: %s" % (status, body))


class TextModelResponseError(TextModelError):
    finish_reason = None          # set when the provider returned no content
    content_limit_exceeded = False
    transport_error = None        # the network error's class name, when the call never got an answer
    certificate_error = False     # that error was an SSL certificate / handshake failure (tls_setup)


def _transport_failure(error, secret):
    """The TextModelResponseError for a network error, naming its class and whether it was a
    certificate failure (Setup and doctor say that plainly instead of "check your internet")."""
    import tls_setup
    failure = TextModelResponseError("transport " + type(error).__name__ + ": " + _short(str(error), secret))
    failure.transport_error = type(error).__name__
    failure.certificate_error = tls_setup.is_certificate_error(error)
    return failure


def _short(text, secret):
    text = text if isinstance(text, str) else repr(text)
    if secret:
        text = text.replace(secret, "[key]")
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[redacted]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[key]", text)
    return " ".join(text.split())[:ERROR_BODY_LIMIT]


def _post(url, data, headers, timeout):
    """(status, body bytes). An HTTP error status is returned, not raised."""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        return error.code, error.read(1 << 16)


def check_connection(timeout=20, transport=None, base_url=None):
    """`sidedoor net-check` and doctor's Secure connection line: one GET of OpenRouter's public model
    list (no key, no credit) through the same urllib path `_post` uses. Returns the HTTP status (any
    answer means the secure connection worked); raises TimeoutError, or TextModelResponseError with
    `transport_error` / `certificate_error` when no answer came. `transport(url, timeout) -> status`
    replaces the network in tests."""
    url = (base_url or BASE_URL).rstrip("/") + "/models"

    def get(url, timeout):
        request = urllib.request.Request(url, headers={"X-Title": "Sidedoor"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read(1 << 10)
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
    try:
        return (transport or get)(url, timeout)
    except TimeoutError:
        raise
    except OSError as error:
        if "timed out" in str(error):
            raise TimeoutError("OpenRouter connection check timed out") from None
        raise _transport_failure(error, None) from None


SLOT_WAIT_SECONDS = 300   # the longest a direct call waits for an "openrouter" pacing slot


def complete(config, prompt, *, system=None, effort="low", max_tokens=4000, timeout=60, transport=None,
             response_format=None, provider=None, api_key=None, content_limit=CONTENT_LIMIT,
             messages=None, tools=None, tool_choice=None, worker=None, max_wait=None, wait_in_timeout=False):
    """`_complete`, with one model_calls row per call that reached the network (C4). `worker` names
    the row when the call is made on a worker's behalf (reply_worker.execute_text), else the caller.
    `max_wait` caps the pacing-slot wait (default SLOT_WAIT_SECONDS); with `wait_in_timeout` the wait
    is spent from `timeout` (the reply tool loop, whose turns share one deadline)."""
    meta = {}
    worker = worker or model_calls.caller()
    # A real call takes an "openrouter" pacing slot for its duration (owner, 2026-10-27); a key under test
    # (`api_key`: Setup, Settings, doctor) is never held back.
    import provider_pacing
    waiting = time.monotonic()
    try:
        lease = provider_pacing.acquire("openrouter", timeout, max_wait=SLOT_WAIT_SECONDS if max_wait is None
                                        else max_wait) if api_key is None else None
    except provider_pacing.SlotWaitTooLong as busy:
        # No slot within SLOT_WAIT_SECONDS: the same short wait a 429 is (2026-10-27), raised the way a 429 is.
        import provider_limits
        raise TextModelHTTPError(429, "PROVIDER_SLOT_WAIT_TOO_LONG") from provider_limits.slot_wait(busy)
    if wait_in_timeout:
        timeout -= time.monotonic() - waiting   # _complete raises TimeoutError when nothing is left
    try:
        return _paced(config, prompt, worker, meta, system=system, effort=effort, max_tokens=max_tokens,
                      timeout=timeout, transport=transport, response_format=response_format, provider=provider,
                      api_key=api_key, content_limit=content_limit, messages=messages, tools=tools,
                      tool_choice=tool_choice)
    finally:
        provider_pacing.release("openrouter", lease)


def _paced(config, prompt, worker, meta, *, system, effort, max_tokens, timeout, transport, response_format,
           provider, api_key, content_limit, messages=None, tools=None, tool_choice=None):
    started = time.monotonic()

    def recorded(outcome, usage=None):
        usage = usage or {}
        model_calls.record("openrouter_direct", outcome, worker=worker,
                           model=meta.get("model") or (settings(config) or {}).get("model"),
                           provider=meta.get("provider"), generation_id=meta.get("id"),
                           prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                           reasoning_tokens=usage.get("reasoning_tokens"), cost=usage.get("cost"),
                           seconds=round(time.monotonic() - started, 3))

    try:
        answer = _complete(config, prompt, system=system, effort=effort, max_tokens=max_tokens, timeout=timeout,
                           transport=transport, response_format=response_format, provider=provider,
                           api_key=api_key, content_limit=content_limit, meta=meta, messages=messages, tools=tools,
                           tool_choice=tool_choice)
    except TimeoutError:
        recorded("timeout")
        raise
    except TextModelHTTPError as error:
        recorded("refused", meta.get("usage"))
        if str(error.status) == "402" and api_key is None:
            # Out of credit (2026-10-23): a capacity wait, not a failed try. The error is raised as
            # before, chained to the wait, so every caller's `provider_limits.wait_of` finds it; a
            # key under test (`api_key`, Setup and doctor) records nothing.
            import provider_limits
            raise error from provider_limits.out_of_credit(str(error))
        if str(error.status) == "429" and api_key is None:
            # Too many requests (2026-10-27): the same short wait as a Codex 429, chained the same way.
            import provider_limits
            raise error from provider_limits.rate_limit(str(error), provider="openrouter")
        raise
    except TextModelResponseError:
        recorded("invalid", meta.get("usage"))
        raise
    recorded("ok", answer["usage"])
    import provider_limits
    # A call that worked ends the out-of-credit banner at once: a real call, and also a key test that
    # passed (Setup's and Settings' Save/Test, doctor --online: each is this call with `api_key`).
    provider_limits.clear(provider_limits.CREDIT_WAIT_FILE)
    if api_key is None:
        import provider_pacing
        provider_pacing.succeeded("openrouter")
    return answer


def _complete(config, prompt, *, system=None, effort="low", max_tokens=4000, timeout=60, transport=None,
              response_format=None, provider=None, api_key=None, content_limit=CONTENT_LIMIT, meta=None,
              messages=None, tools=None, tool_choice=None):
    """One chat-completions call to the configured OpenRouter model.

    Returns {"content", "usage": {prompt_tokens, completion_tokens, reasoning_tokens, cost},
    "model", "finish_reason"}. Raises TextModelHTTPError (status and a short, key-free body) for a
    refused call, TextModelResponseError for an unusable answer and TimeoutError past `timeout`
    seconds in all (the answer is then discarded, as a codex run past its timeout is killed).
    `transport(url, data, headers, timeout) -> (status, body)` replaces the network in tests.
    `response_format` (optional) is sent as given, e.g. a strict json_schema: the structured
    output codex's --output-schema asked for (the outreach send judge, 2026-10-13).
    `provider` (optional) is OpenRouter's provider-routing object, sent as given; without it
    `config["text_model"]["provider"]` is sent when set, else nothing (OpenRouter's default).
    `api_key` (optional) is used instead of the saved key (`key(config)`), so a key can be tried
    before it is saved; a refused key is a TextModelHTTPError with its status (e.g. 401).
    `content_limit` (optional) raises the answer's byte limit for one caller (résumé intake).
    `messages` (optional) is the whole conversation, sent instead of `system` and `prompt`; `tools`
    and `tool_choice` (optional) are OpenAI-style function tools, sent as given (the reply AI's
    tool loop, 2026-10-27). An answer that calls tools may have no content: its `tool_calls` and
    the assistant `message` itself (to send back with the tools' results) are returned.
    """
    chosen = settings(config)
    if not chosen:
        raise ValueError("TEXT_MODEL_NOT_CONFIGURED")
    if timeout <= 0:
        raise TimeoutError("Text model deadline exhausted")
    secret = api_key if api_key is not None else key(config)
    if messages is None:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": chosen["model"], "messages": messages, "reasoning": {"effort": effort},
            "max_tokens": max_tokens, "usage": {"include": True}}
    if tools:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if response_format is not None:
        body["response_format"] = response_format
    routing = provider if provider is not None else chosen.get("provider")
    if isinstance(routing, dict) and routing:
        body["provider"] = routing
    url = (chosen.get("base_url") or BASE_URL).rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer " + secret, "Content-Type": "application/json",
               "X-Title": "Sidedoor"}
    started = time.monotonic()
    try:
        status, raw = (transport or _post)(url, json.dumps(body).encode(), headers, timeout)
    except TimeoutError:
        raise
    except OSError as error:
        if "timed out" in str(error):
            raise TimeoutError("Text model call timed out") from None
        raise _transport_failure(error, secret) from None
    if time.monotonic() - started > timeout:
        raise TimeoutError("Text model call exceeded its deadline")
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if status != 200:
        raise TextModelHTTPError(status, _short(text, secret))
    try:
        answer = json.loads(text)
    except ValueError:
        raise TextModelResponseError("unparsed response: " + _short(text, secret)) from None
    if isinstance(answer, dict) and meta is not None:
        meta.update({key: answer.get(key) for key in ("id", "model", "provider") if isinstance(answer.get(key), str)})
        if isinstance(answer.get("usage"), dict):
            meta["usage"] = {"prompt_tokens": answer["usage"].get("prompt_tokens"),
                             "completion_tokens": answer["usage"].get("completion_tokens"),
                             "cost": answer["usage"].get("cost")}
    error = answer.get("error") if isinstance(answer, dict) else None
    choice = (answer.get("choices") or [None])[0] if isinstance(answer, dict) else None
    error = error or (choice.get("error") if isinstance(choice, dict) else None)
    if error:
        code = error.get("code") if isinstance(error, dict) else None
        raise TextModelHTTPError(code if code is not None else status, _short(json.dumps(error), secret))
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    tool_calls = [call for call in tool_calls if isinstance(call, dict)] if isinstance(tool_calls, list) else []
    if tool_calls and tools:
        content = content if isinstance(content, str) else ""
    elif not isinstance(content, str) or not content.strip():
        error = TextModelResponseError("no content (finish_reason %s)" % (choice or {}).get("finish_reason"))
        error.finish_reason = (choice or {}).get("finish_reason")
        raise error
    if len(content.encode()) > content_limit:
        error = TextModelResponseError("content exceeds %d bytes" % content_limit)
        error.content_limit_exceeded = True
        raise error
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    result = {"content": content, "model": answer.get("model"), "finish_reason": choice.get("finish_reason"),
            "usage": {"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                      "reasoning_tokens": details.get("reasoning_tokens"), "cost": usage.get("cost")}}
    if tools:
        result.update(tool_calls=tool_calls if tool_calls else [], message=message)
    return result


def json_object(content):
    """The JSON value in a model's answer: the whole text, else a fenced block, else the outermost
    {...}. Raises ValueError when none parses; the caller's own validation decides the rest."""
    text = content.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except ValueError:
            pass
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        return json.loads(text[first:last + 1])
    raise ValueError("No JSON object in the model's answer")
