"""Groq chat helper.

Same 4+ attempt pattern as the existing QuoteBot, plus what a free-plan token limit needs:
- the model list is configurable (GROQ_MODELS) and tried in order
- a model that does not exist / is not available to the account (model_not_found) is skipped for an hour
- a rate-limited model (429) is skipped for the time Groq asks for, so the next model is used
- tokens used in the last 60 seconds are tracked PER MODEL (Groq's limits are per model); a request that
  would not fit goes to another model, or waits a few seconds, instead of failing with a 429
`post` and `sleep` can be injected for tests."""
import json
import logging
import re
import threading
import time
from collections import deque

import requests
from . import config

URL = 'https://api.groq.com/openai/v1/chat/completions'
DEAD_SECONDS = 3600
_lock = threading.Lock()
_dead, _cool, _used = {}, {}, {}      # model -> until_ts | until_ts | deque[(ts, tokens)]
_boost, _no_effort = {}, set()        # model -> extra output tokens after an empty answer | models that rejected reasoning_effort


class GroqError(Exception):
    pass


def reset_state():
    with _lock:
        _dead.clear(); _cool.clear(); _used.clear(); _boost.clear(); _no_effort.clear()


def extract_json(raw):
    raw = re.sub(r'<think>.*?</think>', '', raw or '', flags=re.DOTALL).strip()
    if '```' in raw:
        parts = raw.split('```')
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith('json'):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'(\{.*\}|\[.*\])', raw, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        raise


def _retry_after(text):
    """Seconds Groq asks us to wait: 'Please try again in 7.17s' / '1m3.5s' / '350ms'."""
    m = re.search(r'try again in\s+(?:(\d+)m)?\s*([\d.]+)(ms|s)', str(text))
    if not m:
        return 20.0
    mins, val, unit = m.groups()
    secs = float(val) / (1000 if unit == 'ms' else 1)
    return secs + 60 * int(mins or 0)


def _window(model, now):
    dq = _used.setdefault(model, deque())
    while dq and now - dq[0][0] >= 60:
        dq.popleft()
    return sum(t for _, t in dq)


def _is_reasoning(model):
    return model.startswith('openai/gpt-oss') or 'qwen3' in model or 'deepseek-r1' in model


def _limit():
    return int(config.GROQ_TPM_LIMIT * config.GROQ_TPM_SAFETY)


def _pick(est_for, now):
    """(model, 0) that can take `est` tokens right now, else (None, seconds to wait) or (None, None) if none usable."""
    waits = []
    for m in config.GROQ_MODELS:
        if _dead.get(m, 0) > now:
            continue
        if _cool.get(m, 0) > now:
            waits.append(_cool[m] - now)
            continue
        used, est = _window(m, now), est_for(m)
        if used + est <= _limit():
            return m, 0
        need, acc = used + est - _limit(), 0
        for ts, t in _used[m]:
            acc += t
            if acc >= need:
                waits.append(ts + 60 - now)
                break
    return (None, min(waits)) if waits else (None, None)


def chat_json(prompt, max_tokens=800, temperature=0.1, expect=None, post=None, sleep=None, api_key=None):
    post = post or requests.post
    sleep = sleep or time.sleep
    key = config.GROQ_API_KEY if api_key is None else api_key
    messages = [{'role': 'user', 'content': prompt}]
    prompt_est = int(len(prompt) / 3.0)                      # conservative: ~3 characters per token
    if prompt_est + 150 > _limit():
        raise GroqError(f'prompt too large for the token limit (~{prompt_est} tokens)')
    room = _limit() - prompt_est - 50                      # most output tokens this request may ask for

    def out_tokens(model):
        """Reasoning models think before answering and that counts against max_tokens: give them headroom."""
        extra = (config.GROQ_REASONING_HEADROOM if _is_reasoning(model) else 0) + _boost.get(model, 0)
        return max(150, min(max_tokens + extra, room))

    est_for = lambda m: prompt_est + out_tokens(m)
    last_error = 'No response received'

    for attempt in range(max(4, len(config.GROQ_MODELS) + 2)):
        with _lock:
            model, wait = _pick(est_for, time.time())
        if model is None:
            if wait is None:
                raise GroqError(f'no usable Groq model (all unavailable). Last error: {last_error}')
            sleep(min(max(wait, 1), 25))                    # all models busy: wait for the window to free up
            last_error = 'waiting for the per-minute token window'
            continue
        try:
            body = {'model': model, 'messages': messages, 'temperature': temperature, 'max_tokens': out_tokens(model)}
            effort = model.startswith('openai/gpt-oss') and config.GROQ_REASONING_EFFORT and model not in _no_effort
            if effort:
                body['reasoning_effort'] = config.GROQ_REASONING_EFFORT        # less hidden thinking = fewer tokens
            r = post(URL, headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                     json=body, timeout=60)
            d = r.json()
            err = d.get('error') if isinstance(d, dict) else None
            blob = json.dumps(err) if err else ''
            if r.status_code == 429 or (err and ('rate' in blob.lower() and 'limit' in blob.lower())):
                with _lock:
                    _cool[model] = time.time() + max(5.0, min(_retry_after(blob), 60.0))
                last_error = f'{model} rate limited'
                continue                                     # next attempt picks another model
            if err and effort and 'reasoning' in blob.lower():
                _no_effort.add(model)                         # this model does not accept the setting: stop sending it
                last_error = f'{model} rejected reasoning_effort'
                continue
            if err and ('model_not_found' in blob or 'does not exist' in blob or 'access' in blob.lower()
                        or r.status_code in (403, 404)):
                with _lock:
                    _dead[model] = time.time() + DEAD_SECONDS
                logging.warning('Groq model %s is not available to this account; skipping it for an hour', model)
                last_error = f'{model} not available'
                continue
            if err:
                last_error = blob[:200]
                sleep(3)
                continue
            if not d.get('choices'):
                last_error = str(d)[:200]
                sleep(3)
                continue
            raw = (d['choices'][0].get('message', {}).get('content') or '').strip()
            finish = d['choices'][0].get('finish_reason')
            used = (d.get('usage') or {}).get('total_tokens') or est_for(model)
            with _lock:
                _used.setdefault(model, deque()).append((time.time(), used))     # thinking tokens count too
            if not raw or finish == 'length':
                # The model ran out of output tokens (usually spent on hidden thinking). Same request again would fail
                # the same way: give this model more room next time and let another model answer now.
                with _lock:
                    _boost[model] = min(_boost.get(model, 0) + 600, 2400)
                    now_ts = time.time()
                    if any(m != model and _dead.get(m, 0) <= now_ts for m in config.GROQ_MODELS):
                        _cool[model] = now_ts + 15           # another model exists: let it answer, rest this one
                last_error = f'{model}: empty or cut-off answer (finish_reason={finish})'
                continue
            parsed = extract_json(raw)
            if expect and not isinstance(parsed, expect):
                last_error = f'Unexpected JSON type {type(parsed).__name__}'
                sleep(2)
                continue
            return parsed
        except json.JSONDecodeError:
            last_error = 'JSON decode error'
            sleep(2)
        except requests.exceptions.Timeout:
            last_error = 'Timeout'
            sleep(3)
        except Exception as e:  # keep trying
            last_error = str(e)
            sleep(2)

    raise GroqError(f'AI service unavailable. Last error: {last_error}')
