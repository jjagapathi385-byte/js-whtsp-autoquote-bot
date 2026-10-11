"""Groq chat helper with the same fallback pattern as the existing QuoteBot:
4 attempts via range(4), models openai/gpt-oss-120b -> qwen/qwen3-32b -> llama-3.1-8b-instant,
rate-limit back-off, markdown-fence stripping. `post` and `sleep` can be injected for tests."""
import json
import re
import time
import requests
from . import config

MODELS = ['openai/gpt-oss-120b', 'qwen/qwen3-32b', 'llama-3.1-8b-instant']
URL = 'https://api.groq.com/openai/v1/chat/completions'


class GroqError(Exception):
    pass


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


def chat_json(prompt, max_tokens=2000, temperature=0.1, expect=None, post=None, sleep=None, api_key=None,
              messages=None, models=None):
    post = post or requests.post
    sleep = sleep or time.sleep
    key = config.GROQ_API_KEY if api_key is None else api_key
    last_error = 'No response received'
    models = models or MODELS
    messages = messages or [{'role': 'user', 'content': prompt}]

    for attempt in range(4):
        model = models[min(attempt, len(models) - 1)]
        try:
            r = post(URL, headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                     json={'model': model, 'messages': messages,
                           'temperature': temperature, 'max_tokens': max_tokens}, timeout=60)
            d = r.json()
            if r.status_code == 429 or (isinstance(d.get('error'), dict) and 'rate' in str(d['error']).lower()):
                last_error = 'rate limited'
                sleep(5 * (attempt + 1))
                continue
            if 'error' in d:
                last_error = str(d['error'])[:200]
                sleep(3)
                continue
            if 'choices' not in d or not d['choices']:
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
            return parsed
        except json.JSONDecodeError:
            last_error = 'JSON decode error'
            sleep(2)
        except requests.exceptions.Timeout:
            last_error = 'Timeout'
            sleep(3)
        except Exception as e:  # keep trying with the next model
            last_error = str(e)
            sleep(2)

    raise GroqError(f'AI service unavailable. Last error: {last_error}')

