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


class GroqError(Exception):
    pass


def reset_state():
    with _lock:
        _dead.clear(); _cool.clear(); _used.clear()


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


def _limit():
    return int(config.GROQ_TPM_LIMIT * config.GROQ_TPM_SAFETY)


def _pick(est, now):
    """(model, 0) that can take `est` tokens right now, else (None, seconds to wait) or (None, None) if none usable."""
    waits = []
    for m in config.GROQ_MODELS:
        if _dead.get(m, 0) > now:
            continue
        if _cool.get(m, 0) > now:
            waits.append(_cool[m] - now)
            continue
        used = _window(m, now)
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
    max_tokens = max(150, min(max_tokens, _limit() - prompt_est - 50))
    est = prompt_est + max_tokens
    last_error = 'No response received'

    for attempt in range(max(4, len(config.GROQ_MODELS) + 2)):
        with _lock:
            model, wait = _pick(est, time.time())
        if model is None:
            if wait is None:
                raise GroqError(f'no usable Groq model (all unavailable). Last error: {last_error}')
            sleep(min(max(wait, 1), 25))                    # all models busy: wait for the window to free up
            last_error = 'waiting for the per-minute token window'
            continue
        try:
            r = post(URL, headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                     json={'model': model, 'messages': messages, 'temperature': temperature,
                           'max_tokens': max_tokens}, timeout=60)
            d = r.json()
            err = d.get('error') if isinstance(d, dict) else None
            blob = json.dumps(err) if err else ''
            if r.status_code == 429 or (err and ('rate' in blob.lower() and 'limit' in blob.lower())):
                with _lock:
                    _cool[model] = time.time() + max(5.0, min(_retry_after(blob), 60.0))
                last_error = f'{model} rate limited'
                continue                                     # next attempt picks another model
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
            if not raw:
                last_error = 'Empty AI response'
                sleep(3)
                continue
            parsed = extract_json(raw)
            if expect and not isinstance(parsed, expect):
                last_error = f'Unexpected JSON type {type(parsed).__name__}'
                sleep(2)
                continue
            used = (d.get('usage') or {}).get('total_tokens') or est
            with _lock:
                _used.setdefault(model, deque()).append((time.time(), used))
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
