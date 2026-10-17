"""Authenticated handoff from Flask's HTTP thread into PTB's asyncio loop."""
import asyncio
import concurrent.futures
import hashlib
import hmac
import logging
import os
from collections import deque

log=logging.getLogger(__name__)
_application=None
_loop=None
_seen=set()
_seen_order=deque()


def secret():
    explicit=os.getenv('TELEGRAM_WEBHOOK_SECRET')
    if explicit:
        return explicit
    token=os.getenv('TELEGRAM_TOKEN','')
    return hashlib.sha256(('FSK-M-webhook-v1\0'+token).encode()).hexdigest() if token else ''


def attach(application,loop):
    global _application,_loop
    _application=application;_loop=loop
    _seen.clear();_seen_order.clear()


def detach():
    global _application,_loop
    _application=None;_loop=None


async def enqueue(application,data):
    from telegram import Update
    update_id=data.get('update_id')
    if update_id in _seen:
        return
    update=Update.de_json(data,application.bot)
    await application.update_queue.put(update)
    _seen.add(update_id);_seen_order.append(update_id)
    if len(_seen_order)>1000:
        _seen.discard(_seen_order.popleft())
    log.info('WEBHOOK_RECEIVED | update_id=%s',update_id)


def receive(data,provided_secret):
    expected=secret()
    if not expected or not hmac.compare_digest(str(provided_secret or ''),expected):
        return {'error':'unauthorized'},403
    if not isinstance(data,dict) or not isinstance(data.get('update_id'),int):
        return {'error':'invalid_update'},400
    application,loop=_application,_loop
    if application is None or loop is None or not loop.is_running():
        return {'status':'starting'},503
    future=asyncio.run_coroutine_threadsafe(enqueue(application,data),loop)
    try:
        future.result(timeout=3)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return {'status':'retry'},503
    except Exception as exc:
        log.error('WEBHOOK_ENQUEUE_FAILED | type=%s',type(exc).__name__)
        return {'status':'retry'},503
    return {'ok':True},200
