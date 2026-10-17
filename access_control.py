"""Live private-GitHub password rules and persistent per-Telegram-user quotas."""
import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections import deque
from urllib.parse import quote

import requests


class AccessUnavailable(Exception):
    pass


class AccessControl:
    def __init__(self, database):
        self.db = database
        self._rules = {}
        self._loaded = None
        self._retry_at = 0
        self._lock = threading.Lock()
        self._attempts = {}

    def _digest(self, password):
        key = os.environ.get('TELEGRAM_TOKEN', '')
        if not key:
            raise AccessUnavailable('Access configuration unavailable')
        return hmac.new(key.encode(), password.encode(), hashlib.sha256).hexdigest()

    def _parse(self, text):
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError('Duplicate configuration key')
                result[key] = value
            return result
        data = json.loads(text, object_pairs_hook=pairs)
        if not isinstance(data, dict) or set(data) != {'enabled', 'passwords'}:
            raise ValueError('Invalid configuration')
        if type(data['enabled']) is not bool or not isinstance(data['passwords'], dict):
            raise ValueError('Invalid configuration')
        rules = {}
        for password, rule in data['passwords'].items():
            if not isinstance(password, str) or len(password) < 8 or password != password.strip():
                raise ValueError('Passwords must have at least 8 characters and no outer spaces')
            if not isinstance(rule, dict) or set(rule) - {'daily', 'hourly'}:
                raise ValueError('Only daily and hourly limits are supported')
            limits = {'daily': rule.get('daily'), 'hourly': rule.get('hourly')}
            for limit in limits.values():
                if limit is not None and (type(limit) is not int or limit < 0 or limit > 1000000):
                    raise ValueError('Limits must be nonnegative integers or null')
            rules[self._digest(password)] = limits
        return rules if data['enabled'] else {}

    def rules(self):
        with self._lock:
            now = time.monotonic()
            if self._loaded is not None and now - self._loaded < 60:
                return self._rules
            if now < self._retry_at:
                raise AccessUnavailable('Access configuration unavailable')
            try:
                repo = os.environ.get('ACCESS_GITHUB_REPO', '')
                token = os.environ.get('ACCESS_GITHUB_TOKEN', '')
                branch = os.environ.get('ACCESS_GITHUB_BRANCH', 'main')
                if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not token:
                    raise ValueError('Private repository configuration missing')
                headers = {'Authorization': f'Bearer {token}',
                           'Accept': 'application/vnd.github+json',
                           'X-GitHub-Api-Version': '2022-11-28'}
                base = f'https://api.github.com/repos/{repo}'
                with requests.get(base, headers=headers, timeout=(4, 8), allow_redirects=False) as response:
                    response.raise_for_status()
                    if response.json().get('private') is not True:
                        raise ValueError('Password repository must be private')
                headers['Accept'] = 'application/vnd.github.raw+json'
                with requests.get(base + '/contents/access_passwords.json', headers=headers,
                                  params={'ref': branch}, timeout=(4, 8),
                                  allow_redirects=False, stream=True) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_content(8192):
                        raw.extend(chunk)
                        if len(raw) > 262144:
                            raise ValueError('Configuration too large')
                rules = self._parse(raw.decode('utf-8-sig'))
            except Exception as exc:
                self._retry_at = time.monotonic() + 10
                # Do not print response bodies, URLs with credentials, or passwords.
                print(f'ACCESS CONFIG unavailable | {type(exc).__name__}', flush=True)
                raise AccessUnavailable('Access configuration unavailable') from None
            self._rules = rules
            self._loaded = time.monotonic()
            print(f'ACCESS CONFIG refreshed | active_passwords={len(rules)}', flush=True)
            return rules

    @staticmethod
    def _database_failure(operation, exc):
        code = str(getattr(exc, 'code', '') or '')
        code = code if re.fullmatch(r'[A-Za-z0-9_]{1,20}', code) else ''
        print(f'ACCESS DB unavailable | operation={operation} | {type(exc).__name__} | code={code}', flush=True)
        raise AccessUnavailable('Access database unavailable') from None

    def _stored_hash(self, uid):
        rows = self.db.table('fsk_m_access_users').select('password_hash').eq('user_id', uid).limit(1).execute().data
        return rows[0]['password_hash'] if rows else None

    def authorized(self, uid):
        try:
            rules = self.rules()
            digest = self._stored_hash(uid)
            return bool(digest and digest in rules)
        except AccessUnavailable:
            raise
        except Exception as exc:
            self._database_failure('authorization', exc)

    def logout(self, uid):
        try:
            # Keep the row and its quota history; only remove the login credential.
            self.db.table('fsk_m_access_users').update({'password_hash': None}).eq('user_id', uid).execute()
        except Exception as exc:
            self._database_failure('logout', exc)

    def login(self, uid, password):
        now = time.monotonic()
        with self._lock:
            self._attempts = {k: q for k, q in self._attempts.items() if q and now - q[-1] < 60}
            attempts = self._attempts.setdefault(uid, deque())
            while attempts and now - attempts[0] >= 60:
                attempts.popleft()
            if len(attempts) >= 5:
                return 'throttled'
            attempts.append(now)
        digest = self._digest(password)
        if digest not in self.rules():
            return 'invalid'
        try:
            self.db.table('fsk_m_access_users').upsert({'user_id': uid, 'password_hash': digest},
                                                      on_conflict='user_id').execute()
        except Exception as exc:
            self._database_failure('login', exc)
        return 'ok'

    def quota_status(self, uid):
        """Read remaining capacity without spending a request; DB clock is authoritative."""
        try:
            rules = self.rules()
            digest = self._stored_hash(uid)
            if digest not in rules:
                return {'authenticated': False}
            rule = rules[digest]
            result = self.db.rpc('fsk_m_access_status', {
                'p_user_id': uid, 'p_password_hash': digest,
                'p_daily': rule['daily'], 'p_hourly': rule['hourly'],
            }).execute().data
            if not isinstance(result, dict) or type(result.get('authenticated')) is not bool:
                raise ValueError('Invalid quota status response')
            return result
        except AccessUnavailable:
            raise
        except Exception as exc:
            self._database_failure('quota_status', exc)

    def reserve(self, uid, request_id):
        """Atomic, idempotent debit immediately before starting a Gemini search."""
        try:
            rules = self.rules()
            digest = self._stored_hash(uid)
            if digest not in rules:
                return {'allowed': False, 'reason': 'auth'}
            rule = rules[digest]
            result = self.db.rpc('fsk_m_reserve_search', {
                'p_user_id': uid, 'p_password_hash': digest, 'p_request_id': request_id,
                'p_daily': rule['daily'], 'p_hourly': rule['hourly'],
            }).execute().data
            if not isinstance(result, dict) or type(result.get('allowed')) is not bool:
                raise ValueError('Invalid access RPC response')
            return result
        except AccessUnavailable:
            raise
        except Exception as exc:
            # A network timeout can hide a committed debit. Retry the SAME id only.
            self._database_failure('reservation', exc)


def quota_text(status):
    """Plain Persian text; unknown status must never be described as unlimited."""
    if not isinstance(status, dict) or status.get('authenticated') is not True:
        return '🎟 نمایش سهمیه فعلاً ممکن نیست.'
    if 'daily_limit' not in status or 'hourly_limit' not in status:
        return '🎟 نمایش سهمیه فعلاً ممکن نیست.'
    daily, hourly = status['daily_limit'], status['hourly_limit']
    if daily is None and hourly is None:
        return '♾ دسترسی نامحدود'
    parts = []
    for limit, key, label in ((daily, 'daily_remaining', 'امروز'),
                              (hourly, 'hourly_remaining', 'در ۶۰ دقیقهٔ اخیر')):
        if limit is None:
            continue
        remaining = status.get(key)
        if type(limit) is not int or type(remaining) is not int or not 0 <= remaining <= limit:
            return '🎟 نمایش سهمیه فعلاً ممکن نیست.'
        parts.append(f'{label}: {remaining} از {limit} جست‌وجو')
    text = '🎟 سهمیهٔ باقی‌مانده — ' + ' • '.join(parts)
    return text.translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))
