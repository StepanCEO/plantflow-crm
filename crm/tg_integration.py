import logging
import time

import requests

from django.conf import settings
from django.utils import timezone

from .models import Client, Message

logger = logging.getLogger(__name__)

TG_CHANNEL = 'Telegram'


def tg_request(method, params=None):
    token = getattr(settings, 'TG_BOT_TOKEN', '')
    if not token:
        logger.warning('TG_BOT_TOKEN not configured')
        return None

    url = f'https://api.telegram.org/bot{token}/{method}'
    try:
        resp = requests.post(url, data=params or {}, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if not result.get('ok'):
            logger.error(f'TG API error: {result}')
            return None
        return result.get('result')
    except requests.RequestException as e:
        logger.error(f'TG request failed: {e}')
        return None


def poll_tg_messages():
    token = getattr(settings, 'TG_BOT_TOKEN', '')
    if not token:
        return {'status': 'error', 'message': 'TG token not configured', 'imported': 0}

    updates = tg_request('getUpdates', {
        'timeout': 5,
        'allowed_updates': ['message'],
    })
    if not updates:
        return {'status': 'ok', 'message': 'No updates', 'imported': 0}

    imported = 0
    errors = 0
    last_update_id = 0

    for update in updates:
        try:
            update_id = update.get('update_id', 0)
            if update_id > last_update_id:
                last_update_id = update_id

            message = update.get('message', {})
            chat = message.get('chat', {})
            chat_id = chat.get('id')
            chat_type = chat.get('type', '')

            if chat_type not in ('private', 'group', 'supergroup'):
                continue

            text = message.get('text', '')
            if not text:
                continue

            from_user = message.get('from', {})
            user_id = from_user.get('id')
            first_name = from_user.get('first_name', '')
            last_name = from_user.get('last_name', '')
            username = from_user.get('username', '')
            author_name = f'{first_name} {last_name}'.strip() or username or f'Пользователь TG (id{user_id})'
            contact = f'@{username}' if username else f'tg://user?id={user_id}'

            existing = Message.objects.filter(
                channel=TG_CHANNEL,
                contact=contact,
                text=text,
                created_at__gte=timezone.now() - timezone.timedelta(hours=24),
            ).first()
            if existing:
                continue

            client = _find_or_create_tg_client(author_name, contact, user_id)

            Message.objects.create(
                channel=TG_CHANNEL,
                direction=Message.Direction.INBOUND,
                client=client,
                author_name=author_name,
                contact=contact,
                text=text,
                unread=True,
            )

            client.history = (client.history or []) + [
                {'type': 'message', 'text': f'Входящее через Telegram: {text[:80]}', 'at': timezone.now().isoformat()}
            ]
            client.source = 'Telegram'
            client.save(update_fields=['history', 'source', 'updated_at'])

            imported += 1

        except Exception as e:
            logger.error(f'Failed to process TG update: {e}')
            errors += 1

    if last_update_id:
        tg_request('getUpdates', {'offset': last_update_id + 1})

    return {
        'status': 'ok',
        'message': f'Imported {imported} TG messages ({errors} errors)',
        'imported': imported,
        'errors': errors,
    }


def _find_or_create_tg_client(name, contact, tg_user_id):
    if tg_user_id:
        existing = Message.objects.filter(contact=contact).exclude(client=None).values('client').first()
        if existing:
            client = Client.objects.filter(pk=existing['client']).first()
            if client:
                return client

    existing = Client.objects.filter(name__iexact=name).first()
    if existing:
        return existing

    client = Client.objects.create(
        name=name or 'Клиент из Telegram',
        phone='',
        source='Telegram',
        status=Client.Status.UNKNOWN,
    )
    client.history = [
        {'type': 'import', 'text': 'Создан из сообщения Telegram', 'at': timezone.now().isoformat()}
    ]
    client.save(update_fields=['history', 'updated_at'])
    return client
