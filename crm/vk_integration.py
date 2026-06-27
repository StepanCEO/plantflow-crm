import json
import logging
import time

import requests

from django.conf import settings
from django.utils import timezone

from .models import Client, Message

logger = logging.getLogger(__name__)

VK_API_BASE = 'https://api.vk.com/method/'
VK_API_VERSION = '5.199'
VK_CHANNEL = 'VK'


def _vk_request(method, params=None):
    token = getattr(settings, 'VK_API_TOKEN', '')
    if not token:
        logger.warning('VK_API_TOKEN not configured')
        return None

    data = {
        'access_token': token,
        'v': VK_API_VERSION,
        **(params or {}),
    }

    try:
        resp = requests.post(f'{VK_API_BASE}{method}', data=data, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if 'error' in result:
            logger.error(f'VK API error: {result["error"]}')
            return None
        return result.get('response')
    except requests.RequestException as e:
        logger.error(f'VK request failed: {e}')
        return None


def _find_or_create_client(name, contact, vk_user_id=None):
    if contact:
        client = Client.objects.filter(phone=contact).first()
        if client:
            return client

    if vk_user_id:
        existing = Message.objects.filter(contact=f'vk.com/id{vk_user_id}').exclude(client=None).values('client').first()
        if existing:
            client = Client.objects.filter(pk=existing['client']).first()
            if client:
                return client

    existing = Client.objects.filter(name__iexact=name).first()
    if existing:
        return existing

    client = Client.objects.create(
        name=name or 'Клиент из VK',
        phone=contact or '',
        source='VK',
        status=Client.Status.UNKNOWN,
    )
    client.history = [
        {'type': 'import', 'text': 'Создан из сообщения VK', 'at': timezone.now().isoformat()}
    ]
    client.save(update_fields=['history', 'updated_at'])
    return client


def poll_vk_messages():
    token = getattr(settings, 'VK_API_TOKEN', '')
    if not token:
        return {'status': 'error', 'message': 'VK token not configured', 'imported': 0}

    group_id = getattr(settings, 'VK_GROUP_ID', '')
    params = {'count': 20, 'filter': 'unread'}
    if group_id:
        params['group_id'] = group_id

    response = _vk_request('messages.getConversations', params)
    if not response:
        return {'status': 'error', 'message': 'VK API returned no response', 'imported': 0}

    items = response.get('items', [])
    if not items:
        return {'status': 'ok', 'message': 'No new conversations', 'imported': 0}

    imported = 0
    errors = 0

    for item in items:
        try:
            conversation = item.get('conversation', {})
            peer = conversation.get('peer', {})
            peer_id = peer.get('id')

            if not peer_id:
                continue

            last_msg = item.get('last_message', {})
            from_id = last_msg.get('from_id')

            if from_id and from_id > 0:
                user_info = _vk_request('users.get', {
                    'user_ids': from_id,
                    'fields': 'first_name,last_name',
                })
            else:
                user_info = None

            if user_info and isinstance(user_info, list) and len(user_info) > 0:
                user_data = user_info[0]
                first_name = user_data.get('first_name', '')
                last_name = user_data.get('last_name', '')
                author_name = f'{first_name} {last_name}'.strip() or 'Пользователь VK'
            else:
                author_name = f'Пользователь VK (id{from_id})' if from_id else 'Пользователь VK'

            contact = f'vk.com/id{from_id}' if from_id else ''
            text = last_msg.get('text', '') if last_msg else ''

            if not text:
                continue

            existing = Message.objects.filter(
                channel=VK_CHANNEL,
                contact=contact,
                text=text,
                created_at__gte=timezone.now() - timezone.timedelta(hours=24),
            ).first()
            if existing:
                continue

            client = _find_or_create_client(
                name=author_name,
                contact=contact,
                vk_user_id=from_id,
            )

            Message.objects.create(
                channel=VK_CHANNEL,
                direction=Message.Direction.INBOUND,
                client=client,
                author_name=author_name,
                contact=contact,
                text=text,
                unread=True,
            )

            client.history = (client.history or []) + [
                {
                    'type': 'message',
                    'text': f'Входящее через VK: {text[:80]}',
                    'at': timezone.now().isoformat(),
                }
            ]
            client.source = 'VK'
            client.save(update_fields=['history', 'source', 'updated_at'])

            imported += 1
            time.sleep(0.5)

        except Exception as e:
            logger.error(f'Failed to process VK conversation: {e}')
            errors += 1

    return {
        'status': 'ok',
        'message': f'Imported {imported} VK messages ({errors} errors)',
        'imported': imported,
        'errors': errors,
    }
