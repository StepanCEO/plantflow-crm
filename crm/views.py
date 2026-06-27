from __future__ import annotations

import csv
import io
import os
import random
from datetime import date, datetime, timedelta

import boto3
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .models import AuditEntry, Client, ClockEvent, DictionaryEntry, EmployeeProfile, FraudEvent, KnowledgeArticle, Message, NewsItem, Order, Product, ScriptRule, Task, UploadedFile


ROLE_SECTIONS = {
    EmployeeProfile.Role.ADMIN: ['dashboard', 'inbox', 'clients', 'tasks', 'products', 'knowledge', 'analytics', 'admin'],
    EmployeeProfile.Role.FRONT: ['dashboard', 'inbox', 'clients', 'tasks', 'knowledge', 'analytics'],
    EmployeeProfile.Role.BACK: ['dashboard', 'tasks', 'products', 'knowledge', 'analytics'],
    EmployeeProfile.Role.HYBRID: ['dashboard', 'inbox', 'clients', 'tasks', 'products', 'knowledge', 'analytics'],
    EmployeeProfile.Role.CONTENT: ['dashboard', 'inbox', 'knowledge', 'analytics'],
    EmployeeProfile.Role.LOCOMOTIVE: ['dashboard', 'tasks', 'products', 'knowledge', 'analytics'],
}

NAV_LABELS = {
    'dashboard': 'Дашборд',
    'inbox': 'Единое окно',
    'clients': 'Клиенты',
    'tasks': 'Тикеты',
    'products': 'Склад и 1С',
    'knowledge': 'Обучение',
    'analytics': 'Аналитика',
    'admin': 'Админка',
}

DEFAULT_KNOWLEDGE_LIBRARY = [
    {'role': 'front', 'title': 'Приветствие клиента', 'body': 'Сначала здороваемся, уточняем запрос, канал и желаемый срок. Не перегружаем ответ лишними словами.'},
    {'role': 'front', 'title': 'Если товара нет в наличии', 'body': 'Извиняемся, говорим, что сейчас товара нет, предлагаем под заказ, альтернативу или похожий вариант, а затем фиксируем follow-up.'},
    {'role': 'front', 'title': 'Как отвечать по доставке', 'body': 'Сразу называем доступное окно доставки, стоимость и условия. Если нужно уточнение адреса, задаём один короткий вопрос.'},
    {'role': 'front', 'title': 'Как работать с возражением по цене', 'body': 'Сначала подтверждаем запрос, потом объясняем ценность: свежесть, размер, подбор, упаковка и сервис.'},
    {'role': 'hybrid', 'title': 'Перевод клиента на другой канал', 'body': 'Если вопрос требует фото, счета или долгого согласования, переводим клиента в удобный канал и фиксируем это в карточке.'},
    {'role': 'hybrid', 'title': 'Работа с жалобой', 'body': 'Сначала принимаем эмоцию клиента, потом извиняемся, фиксируем проблему и сразу предлагаем конкретное действие.'},
    {'role': 'content', 'title': 'Сценарий для корпоративного клиента', 'body': 'Уточняем бюджет, срок, формат доставки, состав и нужен ли счёт. После этого собираем КП.'},
    {'role': 'back', 'title': 'Синхронизация с 1С', 'body': 'Проверяем остатки, цены и расхождения. Если есть конфликт, сначала корректируем справочник, потом запускаем повторный импорт.'},
    {'role': 'back', 'title': 'Товар в производстве', 'body': 'Если товар ещё не готов, отмечаем его в листе производства и указываем ожидаемую дату поступления.'},
    {'role': 'locomotive', 'title': 'Ответы в нерабочее время', 'body': 'Сообщаем график, адрес и предлагаем оставить контакт, чтобы вернуться к вопросу в рабочее время.'},
    {'role': 'admin', 'title': 'Как проводить быстрый тест', 'body': 'Администратор запускает тест, сотрудник отвечает коротко и по делу, после чего система фиксирует результат.'},
]

DEFAULT_AUTO_SCRIPTS = [
    {'trigger': 'нет в наличии', 'answer': 'Извините, сейчас этого товара нет в наличии. Могу предложить похожий вариант или оформить под заказ.'},
    {'trigger': 'доставка', 'answer': 'Подскажите адрес, и я сразу сориентирую по времени и стоимости доставки.'},
    {'trigger': 'адрес', 'answer': 'Мы находимся по адресу: Москва, ул. Листовая, 17. Работаем ежедневно с 9:00 до 21:00.'},
    {'trigger': 'цена', 'answer': 'Сейчас уточню актуальную цену и сразу вернусь с ответом.'},
    {'trigger': 'под заказ', 'answer': 'Да, можем оформить под заказ. Напишите удобный срок, и мы уточним наличие у поставщика.'},
]

QUIZ_BANK = [
    {
        'question': 'Какой код нужен сотруднику для входа?',
        'answer': '246810',
        'acceptable': ['246810'],
    },
    {
        'question': 'Что отвечаем, если товара нет в наличии?',
        'answer': 'Извините, сейчас этого товара нет в наличии. Могу предложить похожий вариант или оформить под заказ.',
        'acceptable': ['под заказ', 'извините', 'предложить похожий', 'могу предложить вам другое', 'нет в наличии'],
    },
    {
        'question': 'Что делаем, если клиент просит доставку?',
        'answer': 'Уточняем адрес, время и сразу называем доступное окно доставки.',
        'acceptable': ['уточняем адрес', 'время', 'окно доставки', 'доставку'],
    },
    {
        'question': 'Что делать, если клиент жалуется?',
        'answer': 'Сначала извиняемся, затем фиксируем проблему и предлагаем конкретное решение.',
        'acceptable': ['извиняемся', 'фиксируем проблему', 'предлагаем решение', 'жалуется'],
    },
]


def _fmt_money(value):
    return f"{int(value or 0):,}".replace(',', ' ') + ' ₽'


def _fmt_dt(value):
    if not value:
        return ''
    return timezone.localtime(value).strftime('%d.%m, %H:%M')


def _status_class(value):
    mapping = {
        'buyer': 'good',
        'lead': 'warn',
        'unknown': 'info',
        'done': 'good',
        'in_progress': 'info',
        'waiting': 'warn',
        'new': 'info',
        'critical': 'danger',
        'low': 'warn',
        'ok': 'good',
    }
    return mapping.get(value, 'info')


def _current_role(user: User | None):
    if not user or not user.is_authenticated:
        return None
    profile = getattr(user, 'profile', None)
    if profile:
        return profile.role
    return EmployeeProfile.Role.ADMIN if user.is_staff else EmployeeProfile.Role.FRONT


def _allowed_sections(user):
    return ROLE_SECTIONS.get(_current_role(user), [])


def _base_context(request):
    user = request.user
    role = _current_role(user)
    return {
        'current_role': role,
        'allowed_sections': _allowed_sections(user),
        'nav_labels': NAV_LABELS,
        'current_user_profile': getattr(user, 'profile', None) if user.is_authenticated else None,
        'status_class': _status_class,
        'fmt_money': _fmt_money,
        'fmt_dt': _fmt_dt,
    }


def _client_status(client: Client):
    if client.status == Client.Status.BUYER or client.purchase_count > 0 or client.bank_purchases:
        return Client.Status.BUYER
    if client.phone or client.email:
        return Client.Status.LEAD
    return Client.Status.UNKNOWN


def _selected_id(request, name, fallback=None):
    value = request.GET.get(name) or request.POST.get(name) or fallback
    return value


def _log_action(request, action, before='', after=''):
    AuditEntry.objects.create(
        actor=request.user.get_full_name() or request.user.username or 'system',
        ip_address=request.META.get('REMOTE_ADDR', '127.0.0.1'),
        action=action,
        before=str(before)[:8000],
        after=str(after)[:8000],
    )


def _generate_code():
    return str(random.randint(100000, 999999))


def _lookup_user_by_email(email):
    user = User.objects.filter(email__iexact=email).first()
    if user:
        return user
    profile = EmployeeProfile.objects.filter(work_email__iexact=email).select_related('user').first()
    if profile:
        return profile.user
    return None


def _send_code_email(email, code):
    send_mail(
        subject='Код входа в PlantFlow CRM',
        message=f'Ваш код для входа: {code}\n\nКод действителен 5 минут.',
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


def login_view(request):
    if request.user.is_authenticated:
        return redirect('crm:dashboard')

    error = None
    code_sent = request.session.get('login_email')

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'send_code':
            email = request.POST.get('email', '').strip().lower()
            user = _lookup_user_by_email(email)
            if not user:
                error = 'Пользователь с таким email не найден.'
            else:
                code = _generate_code()
                request.session['login_code'] = code
                request.session['login_email'] = email
                request.session['login_expiry'] = (timezone.now() + timedelta(minutes=5)).isoformat()
                try:
                    _send_code_email(email, code)
                    code_sent = email
                except Exception as e:
                    error = f'Ошибка отправки письма: {e}'

        elif action == 'verify_code':
            input_code = request.POST.get('code', '').strip()
            stored_code = request.session.get('login_code')
            stored_email = request.session.get('login_email')
            expiry_str = request.session.get('login_expiry')

            if not stored_code or not stored_email or not expiry_str:
                error = 'Сначала запросите код.'
            elif input_code != stored_code:
                error = 'Неверный код.'
            elif timezone.now() > timezone.datetime.fromisoformat(expiry_str):
                error = 'Код истёк. Запросите новый.'
                request.session.pop('login_code', None)
                request.session.pop('login_expiry', None)
            else:
                user = _lookup_user_by_email(stored_email)
                if user:
                    login(request, user)
                    _log_action(request, 'Login by code', before='anonymous', after=user.username)
                    request.session.pop('login_code', None)
                    request.session.pop('login_email', None)
                    request.session.pop('login_expiry', None)
                    return redirect('crm:dashboard')
                error = 'Пользователь не найден.'

    return render(request, 'crm/login.html', {
        **_base_context(request),
        'error': error,
        'code_sent': code_sent,
    })


def logout_view(request):
    if request.user.is_authenticated:
        _log_action(request, 'Logout', before=request.user.username, after='anonymous')
    logout(request)
    return redirect('crm:login')


@login_required
def dashboard(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')
        handler = {
            'create_client': _create_or_update_client,
            'create_task': _create_task,
            'send_message': _send_message,
            'sync_1c': _sync_1c,
            'poll_avito': _poll_avito,
            'poll_vk': _poll_vk,
            'poll_tg': _poll_tg,
            'sync_bank': _sync_bank,
            'create_user': _create_user,
            'update_user': _update_user,
            'delete_user': _delete_user,
            'import_csv': _import_csv_users,
            'generate_follow_up': _generate_follow_up,
            'quick_test_start': _quick_test_start,
            'quick_test_answer': _quick_test_answer,
            'quick_test_reset': _quick_test_reset,
            'simulate_incoming': _simulate_incoming,
            'dict_add': _dict_add,
            'dict_delete': _dict_delete,
            'upload_file': _upload_file,
            'wishlist_trigger': _wishlist_trigger,
            'save_article': _save_article,
            'delete_article': _delete_article,
            'clock_in': _clock_toggle,
            'clock_out': _clock_toggle,
            'reassign_task': _reassign_task,
            'export_clients_csv': _export_clients_csv,
            'export_products_csv': _export_products_csv,
            'save_script': _save_script,
            'delete_script': _delete_script,
            'create_order': _create_order,
            'update_order_status': _update_order_status,
            'edit_order': _edit_order,
            'delete_order': _delete_order,
        }.get(action)
        if handler:
            try:
                response = handler(request)
                if response:
                    return response
            except Exception as e:
                messages.error(request, f'Ошибка: {e}')
                return redirect(reverse('crm:dashboard'))

    clients_qs = Client.objects.only('id', 'name', 'one_c_id', 'phone', 'email', 'status', 'source', 'updated_at').all()
    messages_qs = Message.objects.select_related('client', 'assigned_to').only(
        'id', 'channel', 'direction', 'author_name', 'contact', 'text', 'unread', 'created_at',
        'client__name', 'client__id', 'assigned_to__id', 'assigned_to__username',
    ).all()
    tasks_qs = Task.objects.select_related('client', 'assigned_to').only(
        'id', 'title', 'priority', 'status', 'origin', 'due_at', 'created_at',
        'client__name', 'client__id', 'assigned_to__id', 'assigned_to__username',
    ).all()
    products_qs = Product.objects.only('id', 'name', 'parent', 'sku', 'stock', 'reserve', 'price', 'in_production', 'status', 'kind').all()
    news_qs = NewsItem.objects.all()[:3]
    knowledge_qs = list(KnowledgeArticle.objects.all())
    audit_qs = AuditEntry.objects.all()[:10]

    selected_pk = _selected_id(request, 'client')
    selected_client = None
    if selected_pk:
        selected_client = Client.objects.prefetch_related('messages', 'tasks', 'orders').filter(pk=selected_pk).first()
    if not selected_client:
        selected_client = Client.objects.prefetch_related('messages', 'tasks', 'orders').first()
    selected_task = tasks_qs.filter(pk=_selected_id(request, 'task')).first() or tasks_qs.first()
    selected_product = products_qs.filter(pk=_selected_id(request, 'product')).first() or products_qs.first()
    inbox_channel = request.GET.get('channel', 'all')
    search = request.GET.get('q', '').strip()
    task_filter = request.GET.get('task_filter', 'all')
    knowledge_role = request.GET.get('knowledge_role', 'all')
    status_filter = request.GET.get('client_status', 'all')

    if inbox_channel != 'all':
        messages_qs = messages_qs.filter(channel=inbox_channel)
    if search:
        clients_qs = clients_qs.filter(
            Q(name__icontains=search)
            | Q(phone__icontains=search)
            | Q(email__icontains=search)
            | Q(source__icontains=search)
            | Q(tags__icontains=search)
            | Q(interests__icontains=search)
        )
        messages_qs = messages_qs.filter(
            Q(author_name__icontains=search)
            | Q(contact__icontains=search)
            | Q(text__icontains=search)
            | Q(channel__icontains=search)
        )
        tasks_qs = tasks_qs.filter(
            Q(title__icontains=search)
            | Q(origin__icontains=search)
            | Q(client__name__icontains=search)
        )
        products_qs = products_qs.filter(
            Q(name__icontains=search)
            | Q(parent__icontains=search)
            | Q(sku__icontains=search)
        )
    if status_filter in ['buyer', 'lead', 'unknown']:
        clients_qs = clients_qs.filter(status=status_filter)

    client_offset = int(request.GET.get('client_offset', '0'))
    if client_offset < 0:
        client_offset = 0

    if task_filter == 'overdue':
        tasks_qs = tasks_qs.filter(status__in=['new', 'in_progress', 'waiting'], due_at__lt=timezone.now())
    elif task_filter in ['new', 'in_progress', 'waiting', 'done']:
        tasks_qs = tasks_qs.filter(status=task_filter)

    if knowledge_role != 'all':
        knowledge_qs = [item for item in knowledge_qs if item.role == knowledge_role]

    knowledge_items = knowledge_qs or []
    if len(knowledge_items) < 8:
        existing_titles = {item.title for item in knowledge_items}
        for item in DEFAULT_KNOWLEDGE_LIBRARY:
            if item['title'] not in existing_titles and (knowledge_role == 'all' or item['role'] == knowledge_role):
                knowledge_items.append(type('KnowledgeItem', (), item)())

    def _sum_amounts(items):
        return sum(float(item.get('amount', 0)) for item in (items or []))

    def _revenue_for_period(start_date):
        total = 0.0
        for client in Client.objects.all():
            for item in (client.purchases or []):
                at = item.get('at', '')
                if at and isinstance(at, str) and len(at) >= 10:
                    try:
                        item_date = datetime.fromisoformat(at).date()
                        if item_date >= start_date:
                            total += float(item.get('amount', 0))
                    except (ValueError, TypeError):
                        total += float(item.get('amount', 0))
            for item in (client.bank_purchases or []):
                if not item.get('matched'):
                    continue
                at = item.get('at', '')
                if at and isinstance(at, str) and len(at) >= 10:
                    try:
                        item_date = datetime.fromisoformat(at).date()
                        if item_date >= start_date:
                            total += float(item.get('amount', 0))
                    except (ValueError, TypeError):
                        if item.get('matched'):
                            total += float(item.get('amount', 0))
        for order in Order.objects.exclude(status='cancelled'):
            od = order.created_at.date()
            if od >= start_date:
                total += float(order.total)
        return total

    now_date = timezone.now().date()
    revenue_today = _revenue_for_period(now_date)
    revenue_week = _revenue_for_period(now_date - timedelta(days=7))
    revenue_month = _revenue_for_period(now_date - timedelta(days=30))
    revenue_6months = _revenue_for_period(now_date - timedelta(days=180))
    revenue_total = _revenue_for_period(now_date - timedelta(days=36500))
    leads_count = Client.objects.filter(status=Client.Status.LEAD).count()
    active_chats = Message.objects.filter(unread=True).count()
    overdue_count = Task.objects.filter(status__in=['new', 'in_progress', 'waiting'], due_at__lt=timezone.now()).count()

    buyers_count = Client.objects.filter(status=Client.Status.BUYER).count()
    unknown_count = Client.objects.filter(status=Client.Status.UNKNOWN).count()
    total_clients = Client.objects.count()
    total_products = Product.objects.count()
    critical_stock = Product.objects.filter(status=Product.StockStatus.CRITICAL).count()
    low_stock = Product.objects.filter(status=Product.StockStatus.LOW).count()

    role = _current_role(request.user)
    god_mode_messages = None
    if role == EmployeeProfile.Role.ADMIN:
        god_mode_messages = Message.objects.select_related('client', 'assigned_to').all()[:50]

    dict_tags = list(DictionaryEntry.objects.filter(dict_type=DictionaryEntry.DictType.TAG)[:50])
    dict_statuses = list(DictionaryEntry.objects.filter(dict_type=DictionaryEntry.DictType.STATUS)[:50])
    dict_interests = list(DictionaryEntry.objects.filter(dict_type=DictionaryEntry.DictType.INTEREST)[:50])

    fraud_events = FraudEvent.objects.select_related('employee').all()[:20]
    uploaded_files = UploadedFile.objects.select_related('uploaded_by').all()[:10]

    in_production_products = [p for p in products_qs if p.in_production > 0]

    current_clock_event = ClockEvent.objects.filter(user=request.user, clock_out__isnull=True).first()
    db_scripts = list(ScriptRule.objects.filter(is_active=True))

    employee_kpi = []
    for u in User.objects.filter(is_active=True).select_related('profile'):
        if getattr(u, 'profile', None):
            msg_count = Message.objects.filter(assigned_to=u).count()
            task_count = Task.objects.filter(assigned_to=u).count()
            done_tasks = Task.objects.filter(assigned_to=u, status='done').count()
            conversion = round(done_tasks / task_count * 100, 1) if task_count else 0
            employee_kpi.append({
                'name': u.get_full_name() or u.username,
                'role': u.profile.get_role_display(),
                'messages': msg_count,
                'tasks': task_count,
                'done': done_tasks,
                'conversion': conversion,
            })

    context = {
        **_base_context(request),
        'page': request.GET.get('page', 'dashboard'),
        'now': timezone.now(),
        'selected_client': selected_client,
        'selected_task': selected_task,
        'selected_product': selected_product,
        'clients': clients_qs[client_offset:client_offset + 50],
        'inbox_messages': messages_qs[:50],
        'tasks': tasks_qs[:50],
        'products': products_qs[:50],
        'client_offset': client_offset,
        'has_more_clients': clients_qs[client_offset + 50:client_offset + 51].exists(),
        'knowledge_items': knowledge_items,
        'news_items': news_qs,
        'audit_items': audit_qs,
        'leads_count': leads_count,
        'active_chats': active_chats,
        'overdue_count': overdue_count,
        'revenue_today': revenue_today,
        'revenue_week': revenue_week,
        'revenue_month': revenue_month,
        'revenue_6months': revenue_6months,
        'revenue_total': revenue_total,
        'message_query': search,
        'task_query': search,
        'client_query': search,
        'channel_filter': inbox_channel,
        'task_filter': task_filter,
        'knowledge_role': knowledge_role,
        'client_status': status_filter,
        'channels': ['all', 'Telegram', 'VK', 'WhatsApp', 'Email', 'Сайт', 'Flowwow', 'Авито'],
        'nav_items': [(key, NAV_LABELS[key]) for key in _allowed_sections(request.user)],
        'sections': _allowed_sections(request.user),
        'all_users': User.objects.select_related('profile').all(),
        'blocked_users': User.objects.filter(is_active=False),
        'scripts': db_scripts + DEFAULT_AUTO_SCRIPTS,
        'quick_test_question': request.session.get('quick_test_question'),
        'dict_tags': dict_tags,
        'dict_statuses': dict_statuses,
        'dict_interests': dict_interests,
        'fraud_events': fraud_events,
        'uploaded_files': uploaded_files,
        'god_mode_messages': god_mode_messages,
        'buyers_count': buyers_count,
        'unknown_count': unknown_count,
        'total_clients': total_clients,
        'total_products': total_products,
        'critical_stock': critical_stock,
        'low_stock': low_stock,
        'in_production_products': in_production_products,
        'employee_kpi': employee_kpi,
        'current_clock_event': current_clock_event,
        'db_scripts': db_scripts,
        'task_status_new': Task.objects.filter(status='new').count(),
        'task_status_in_progress': Task.objects.filter(status='in_progress').count(),
        'task_status_waiting': Task.objects.filter(status='waiting').count(),
        'task_status_done': Task.objects.filter(status='done').count(),
        'order_count': Order.objects.count(),
        'order_revenue': sum(float(o.total) for o in Order.objects.exclude(status='cancelled')),
    }

    return render(request, 'crm/dashboard.html', context)


def _create_or_update_client(request):
    name = request.POST.get('name', '').strip()
    phone = request.POST.get('phone', '').strip() or None
    email = request.POST.get('email', '').strip()
    one_c_id = request.POST.get('one_c_id', '').strip()
    source = request.POST.get('source', '').strip() or 'Сайт'
    preferred_channel = request.POST.get('preferred_channel', '').strip() or source
    tags = [item.strip() for item in request.POST.get('tags', '').split(',') if item.strip()]
    interests = [item.strip() for item in request.POST.get('interests', '').split(',') if item.strip()]
    wish_list = [item.strip() for item in request.POST.get('wish_list', '').split(',') if item.strip()]
    internal_note = request.POST.get('internal_note', '').strip()
    quality = request.POST.get('quality', 'B')
    green_list = request.POST.get('green_list') == 'on'
    black_list = request.POST.get('black_list') == 'on'
    client_id = request.POST.get('client_id') or None

    if not name:
        messages.error(request, 'Укажите имя клиента.')
        return redirect(_redirect_to_client(request, client_id))

    target = Client.objects.filter(pk=client_id).first() if client_id else None
    duplicate = None
    if phone:
        duplicate = Client.objects.filter(phone=phone).exclude(pk=getattr(target, 'pk', None)).first()
    if not duplicate and email:
        duplicate = Client.objects.filter(email__iexact=email).exclude(pk=getattr(target, 'pk', None)).first()

    if duplicate and target:
        _merge_clients(target, duplicate)
        messages.success(request, f'Карточки {target.name} и {duplicate.name} объединены.')
        return redirect(f"{reverse('crm:dashboard')}?client={target.pk}")

    obj = target or Client()
    obj.name = name
    obj.phone = phone
    obj.email = email
    obj.one_c_id = one_c_id
    obj.source = source
    obj.preferred_channel = preferred_channel
    obj.tags = tags
    obj.interests = interests
    obj.wish_list = wish_list
    obj.internal_note = internal_note
    obj.quality = quality
    obj.green_list = green_list
    obj.black_list = black_list
    obj.status = Client.Status.BUYER if (obj.purchases or obj.bank_purchases) else (Client.Status.LEAD if phone or email else Client.Status.UNKNOWN)
    obj.save()
    obj.history = (obj.history or []) + [{'type': 'update', 'text': 'Карточка обновлена вручную', 'at': timezone.now().isoformat()}]
    obj.save(update_fields=['history', 'updated_at'])

    _log_action(request, 'Create/Update client', before=client_id or 'new', after=obj.name)
    messages.success(request, 'Карточка клиента сохранена.')
    return redirect(f"{reverse('crm:dashboard')}?client={obj.pk}")


def _merge_clients(primary: Client, duplicate: Client):
    primary.tags = sorted(set((primary.tags or []) + (duplicate.tags or [])))
    primary.interests = sorted(set((primary.interests or []) + (duplicate.interests or [])))
    primary.discount_cards = sorted(set((primary.discount_cards or []) + (duplicate.discount_cards or [])))
    primary.wish_list = sorted(set((primary.wish_list or []) + (duplicate.wish_list or [])))
    primary.wait_list = sorted(set((primary.wait_list or []) + (duplicate.wait_list or [])))
    primary.bank_purchases = list((primary.bank_purchases or []) + (duplicate.bank_purchases or []))
    primary.history = list((duplicate.history or []) + (primary.history or []))
    primary.internal_note = '\n'.join(filter(None, [primary.internal_note, duplicate.internal_note]))
    primary.save()
    Message.objects.filter(client=duplicate).update(client=primary)
    Task.objects.filter(client=duplicate).update(client=primary)
    duplicate.delete()


def _create_task(request):
    title = request.POST.get('title', '').strip()
    if not title:
        messages.error(request, 'Введите название тикета.')
        return redirect(reverse('crm:dashboard'))
    due_at = request.POST.get('due_at')
    if due_at:
        due_at = datetime.fromisoformat(due_at)
        if timezone.is_naive(due_at):
            due_at = timezone.make_aware(due_at, timezone.get_current_timezone())
    else:
        due_at = timezone.now() + timedelta(hours=1)
    Task.objects.create(
        title=title,
        priority=int(request.POST.get('priority', '3')),
        urgency=request.POST.get('urgency', 'normal'),
        due_at=due_at,
        status=request.POST.get('status', 'new'),
        origin=request.POST.get('origin', Task.Origin.INTERNAL),
        assigned_to=User.objects.filter(pk=request.POST.get('assigned_to')).first(),
        client=Client.objects.filter(pk=request.POST.get('client_id')).first() if request.POST.get('client_id') else None,
        comments=[],
    )
    _log_action(request, 'Create task', before='new', after=title)
    messages.success(request, 'Тикет создан.')
    return redirect(f"{reverse('crm:dashboard')}?page=tasks")


def _send_message(request):
    client = Client.objects.filter(pk=request.POST.get('client_id')).first() if request.POST.get('client_id') else None
    text = request.POST.get('text', '').strip()
    channel = request.POST.get('channel', '').strip() or (client.preferred_channel if client else 'Telegram')
    if not text:
        messages.error(request, 'Введите текст сообщения.')
        return redirect(_redirect_to_client(request, getattr(client, 'pk', None)))
    Message.objects.create(
        channel=channel,
        direction=Message.Direction.OUTBOUND,
        client=client,
        author_name=request.user.get_full_name() or request.user.username,
        contact=client.phone if client else '',
        text=text,
        unread=False,
        assigned_to=request.user,
    )
    if client:
        client.history = (client.history or []) + [{'type': 'message', 'text': f'Отправлено сообщение через {channel}', 'at': timezone.now().isoformat()}]
        client.save(update_fields=['history', 'updated_at'])
    _log_action(request, 'Send message', before=channel, after=text[:120])
    messages.success(request, 'Сообщение отправлено.')
    return redirect(f"{reverse('crm:dashboard')}?page=inbox&client={getattr(client, 'pk', '')}")


def _sync_1c(request):
    count = 0
    for product in Product.objects.all():
        drift = random.randint(-2, 2)
        product.stock = max(0, product.stock + drift)
        product.status = 'critical' if product.stock <= 2 else 'low' if product.stock <= 6 else 'ok'
        product.save(update_fields=['stock', 'status', 'updated_at'])
        count += 1
    Task.objects.create(
        title='Автозадача: проверить остатки после синхронизации 1С',
        priority=4,
        urgency='system',
        due_at=timezone.now(),
        status=Task.Status.NEW,
        origin=Task.Origin.SYSTEM,
        assigned_to=User.objects.filter(username='back').first(),
        comments=[{'author': 'CRM', 'text': 'Создано после синхронизации 1С.', 'at': timezone.now().isoformat()}],
    )
    _log_action(request, 'Sync 1C', before='inventory', after=f'{count} items refreshed')
    messages.success(request, 'Остатки и статусы обновлены.')
    return redirect(f"{reverse('crm:dashboard')}?page=products")


def _poll_avito(request):
    from .avito_parser import poll_avito_mailbox
    result = poll_avito_mailbox()
    status = result.get('status', 'error')
    imported = result.get('imported', 0)
    msg = result.get('message', '')
    if status == 'ok' and imported > 0:
        messages.success(request, f'Авито: импортировано {imported} сообщений.')
        _log_action(request, 'Poll Avito', before='mailbox', after=f'{imported} messages imported')
    elif status == 'ok':
        messages.info(request, 'Авито: новых писем нет.')
    else:
        messages.warning(request, f'Авито: {msg}')
    return redirect(f"{reverse('crm:dashboard')}?page=products")



def _poll_vk(request):
    from .vk_integration import poll_vk_messages
    result = poll_vk_messages()
    status = result.get('status', 'error')
    imported = result.get('imported', 0)
    msg = result.get('message', '')
    if status == 'ok' and imported > 0:
        messages.success(request, f'VK: импортировано {imported} сообщений.')
        _log_action(request, 'Poll VK', before='vk', after=f'{imported} messages imported')
    elif status == 'ok':
        messages.info(request, 'VK: новых сообщений нет.')
    else:
        messages.warning(request, f'VK: {msg}')
    return redirect(f"{reverse('crm:dashboard')}?page=products")



def _poll_tg(request):
    from .tg_integration import poll_tg_messages
    result = poll_tg_messages()
    status = result.get('status', 'error')
    imported = result.get('imported', 0)
    msg = result.get('message', '')
    if status == 'ok' and imported > 0:
        messages.success(request, f'Telegram: импортировано {imported} сообщений.')
        _log_action(request, 'Poll TG', before='telegram', after=f'{imported} messages imported')
    elif status == 'ok':
        messages.info(request, 'Telegram: новых сообщений нет.')
    else:
        messages.warning(request, f'Telegram: {msg}')
    return redirect(f"{reverse('crm:dashboard')}?page=products")


def _sync_bank(request):
    client = next((item for item in Client.objects.all() if any(not purchase.get('matched') for purchase in (item.bank_purchases or []))), None)
    if client:
        bank_purchases = client.bank_purchases or []
        for item in bank_purchases:
            item['matched'] = True
        client.bank_purchases = bank_purchases
        client.history = (client.history or []) + [{'type': 'purchase', 'text': 'Оплата подтверждена по банковской выписке', 'at': timezone.now().isoformat()}]
        client.save(update_fields=['bank_purchases', 'history', 'updated_at'])
    _log_action(request, 'Sync bank CSV', before='unconfirmed', after='matched')
    messages.success(request, 'Банковская выписка обработана.')
    return redirect(f"{reverse('crm:dashboard')}?page=clients")


def _toggle_user(request):
    user = get_object_or_404(User, pk=request.POST.get('user_id'))
    user.is_active = not user.is_active
    user.save(update_fields=['is_active'])
    _log_action(request, 'Toggle user', before=str(not user.is_active), after=str(user.is_active))
    messages.success(request, 'Статус пользователя изменен.')
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _update_user(request):
    user = get_object_or_404(User, pk=request.POST.get('user_id'))
    first_name = request.POST.get('first_name', '').strip()
    last_name = request.POST.get('last_name', '').strip()
    email = request.POST.get('email', '').strip()
    password = request.POST.get('password', '').strip()
    role = request.POST.get('role', '').strip()
    schedule = request.POST.get('schedule', '').strip()
    is_active = request.POST.get('is_active') == 'on'
    if first_name:
        user.first_name = first_name
    if last_name:
        user.last_name = last_name
    if email:
        user.email = email
    if password:
        user.set_password(password)
    user.is_active = is_active
    user.save()
    profile = getattr(user, 'profile', None)
    if profile:
        if role:
            profile.role = role
        if schedule:
            profile.schedule = schedule
        profile.save()
    _log_action(request, 'Update user', before=user.username, after=f'role={role}')
    messages.success(request, f'Пользователь {user.username} обновлён.')
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _create_user(request):
    username = request.POST.get('username', '').strip()
    if not username:
        messages.error(request, 'Имя пользователя обязательно.')
        return redirect(f"{reverse('crm:dashboard')}?page=admin")
    if User.objects.filter(username=username).exists():
        messages.error(request, f'Пользователь {username} уже существует.')
        return redirect(f"{reverse('crm:dashboard')}?page=admin")
    password = request.POST.get('password', 'temp123').strip()
    role = request.POST.get('role', 'front').strip()
    user = User.objects.create_user(username=username, password=password)
    user.first_name = request.POST.get('first_name', '').strip()
    user.last_name = request.POST.get('last_name', '').strip()
    user.email = request.POST.get('email', '').strip()
    user.save()
    EmployeeProfile.objects.create(user=user, role=role, schedule='09:00-18:00')
    _log_action(request, 'Create user', after=username)
    messages.success(request, f'Пользователь {username} создан.')
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _delete_user(request):
    user = get_object_or_404(User, pk=request.POST.get('user_id'))
    username = user.username
    user.delete()
    _log_action(request, 'Delete user', before=username)
    messages.success(request, f'Пользователь {username} удалён.')
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _import_csv_users(request):
    raw_csv = request.POST.get('csv_data', '').strip()
    if not raw_csv:
        messages.error(request, 'CSV пустой.')
        return redirect(f"{reverse('crm:dashboard')}?page=admin")
    reader = csv.DictReader(io.StringIO(raw_csv))
    count = 0
    for row in reader:
        username = row.get('login', '').strip()
        if not username:
            continue
        user, _ = User.objects.get_or_create(username=username)
        user.first_name = row.get('name', '').strip().split(' ')[0] if row.get('name') else user.first_name
        user.last_name = ' '.join(row.get('name', '').strip().split(' ')[1:]) if row.get('name') and len(row.get('name', '').split(' ')) > 1 else user.last_name
        user.email = row.get('email', '').strip()
        if row.get('password'):
            user.set_password(row['password'])
        else:
            user.set_password('temp123')
        user.is_active = str(row.get('active', 'true')).lower() == 'true'
        user.save()
        profile, _ = EmployeeProfile.objects.get_or_create(user=user)
        profile.role = row.get('role', EmployeeProfile.Role.FRONT)
        profile.work_email = row.get('email', '').strip()
        profile.schedule = row.get('schedule', '09:00-18:00')
        profile.save()
        count += 1
    _log_action(request, 'CSV import', before='users', after=f'{count} rows')
    messages.success(request, f'Импортировано {count} пользователей.')
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _generate_follow_up(request):
    client = None
    for c in Client.objects.filter(status=Client.Status.LEAD):
        wl = c.wish_list or []
        if 'Фикус Лирата' in wl:
            client = c
            break
    if client:
        Task.objects.create(
            title=f'Follow-up: продать товар из wishlist для {client.name}',
            priority=2,
            urgency='system',
            due_at=timezone.now(),
            status=Task.Status.NEW,
            origin=Task.Origin.SYSTEM,
            assigned_to=User.objects.filter(username='front').first(),
            client=client,
            comments=[{'author': 'CRM', 'text': 'Сработал триггер по появлению товара в наличии.', 'at': timezone.now().isoformat()}],
        )
    _log_action(request, 'Auto follow-up', before='wishlist', after=getattr(client, 'name', 'none'))
    messages.success(request, 'Автотриггер создан.')
    return redirect(f"{reverse('crm:dashboard')}?page=tasks")


def _quick_test_start(request):
    question_data = random.choice(QUIZ_BANK)
    request.session['quick_test_question'] = question_data['question']
    request.session['quick_test_answer'] = question_data['answer']
    request.session['quick_test_accept'] = question_data['acceptable']
    return redirect(f"{reverse('crm:dashboard')}?page=knowledge")


def _quick_test_answer(request):
    answer = request.POST.get('quick_answer', '').strip().lower()
    expected = request.session.get('quick_test_answer', '').strip().lower()
    acceptable = [item.strip().lower() for item in request.session.get('quick_test_accept', [])]
    if answer and (
        answer == expected
        or any(token and token in answer for token in acceptable)
    ):
        request.session.pop('quick_test_question', None)
        request.session.pop('quick_test_answer', None)
        request.session.pop('quick_test_accept', None)
        messages.success(request, 'Быстрый тест пройден.')
    else:
        messages.error(request, 'Неверный ответ.')
    return redirect(f"{reverse('crm:dashboard')}?page=knowledge")


def _simulate_incoming(request):
    client = Client.objects.order_by('?').first()
    channel = random.choice(['Telegram', 'VK', 'WhatsApp', 'Email', 'Сайт', 'Flowwow', 'Авито'])
    Message.objects.create(
        channel=channel,
        direction=Message.Direction.INBOUND,
        client=client,
        author_name=client.name if client else 'Новый контакт',
        contact=client.phone if client else '',
        text=f'Авто-входящее сообщение от {client.name if client else "нового контакта"}',
        unread=True,
        assigned_to=request.user,
    )
    if client:
        client.history = (client.history or []) + [{'type': 'message', 'text': f'Входящее через {channel}', 'at': timezone.now().isoformat()}]
        client.save(update_fields=['history', 'updated_at'])
    _log_action(request, 'Incoming message', before='random', after=channel)
    messages.success(request, 'Смоделировано входящее сообщение.')
    return redirect(f"{reverse('crm:dashboard')}?page=inbox&client={getattr(client, 'pk', '')}")


def _dict_add(request):
    dict_type = request.POST.get('dict_type', '').strip()
    key = request.POST.get('key', '').strip()
    label = request.POST.get('label', '').strip()
    if dict_type and key:
        DictionaryEntry.objects.update_or_create(
            dict_type=dict_type,
            key=key,
            defaults={'label': label or key},
        )
        _log_action(request, 'Dictionary add', before=dict_type, after=key)
        messages.success(request, f'Элемент «{label or key}» добавлен в словарь.')
    else:
        messages.error(request, 'Укажите тип и ключ словаря.')
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _dict_delete(request):
    entry_id = request.POST.get('entry_id')
    entry = DictionaryEntry.objects.filter(pk=entry_id).first()
    if entry:
        _log_action(request, 'Dictionary delete', before=str(entry), after='removed')
        entry.delete()
        messages.success(request, 'Элемент удалён.')
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _upload_file(request):
    uploaded = request.FILES.get('file')
    tag = request.POST.get('tag', '').strip()
    if not uploaded:
        messages.error(request, 'Выберите файл.')
        return redirect(f"{reverse('crm:dashboard')}?page=admin")

    s3_key = ''
    s3_url = ''
    s3_bucket = settings.AWS_STORAGE_BUCKET_NAME

    if settings.AWS_ACCESS_KEY_ID and settings.AWS_STORAGE_BUCKET_NAME:
        try:
            client = boto3.client(
                's3',
                endpoint_url=settings.AWS_S3_ENDPOINT_URL,
                region_name=settings.AWS_S3_REGION_NAME,
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            )
            s3_key = f'uploads/{timezone.now():%Y/%m/%d}/{uploaded.name}'
            client.upload_fileobj(uploaded, settings.AWS_STORAGE_BUCKET_NAME, s3_key)
            s3_url = f'{settings.AWS_S3_ENDPOINT_URL}/{settings.AWS_STORAGE_BUCKET_NAME}/{s3_key}'
            _log_action(request, 'File upload S3', before='local', after=s3_key)
            messages.success(request, f'Файл «{uploaded.name}» загружен в S3.')
        except Exception as e:
            _log_action(request, 'File upload S3 error', before='error', after=str(e))
            messages.error(request, f'Ошибка загрузки в S3: {e}')
            return redirect(f"{reverse('crm:dashboard')}?page=admin")
    else:
        _log_action(request, 'File upload stub', before='none', after=uploaded.name)
        messages.success(request, f'Файл «{uploaded.name}» загружен локально (S3 не настроено).')

    UploadedFile.objects.create(
        original_name=uploaded.name,
        s3_key=s3_key,
        s3_bucket=s3_bucket,
        s3_url=s3_url,
        file_size=uploaded.size,
        content_type=uploaded.content_type or '',
        tag=tag,
        uploaded_by=request.user if request.user.is_authenticated else None,
    )
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _wishlist_trigger(request):
    now = timezone.now()
    back_user = User.objects.filter(username='back').first()
    front_user = User.objects.filter(username='front').first()
    count = 0
    in_stock_names = set(
        Product.objects.filter(stock__gt=0).values_list('name', flat=True)
    )
    for client in Client.objects.all():
        wl = client.wait_list or []
        if not wl:
            continue
        for wanted in wl:
            if wanted in in_stock_names:
                Task.objects.create(
                    title=f'Товар из листа ожидания в наличии: {wanted} для {client.name}',
                    priority=2,
                    urgency='system',
                    due_at=now,
                    status=Task.Status.NEW,
                    origin=Task.Origin.SYSTEM,
                    assigned_to=front_user or back_user,
                    client=client,
                    comments=[{'author': 'CRM', 'text': f'Товар «{wanted}» появился в наличии.', 'at': now.isoformat()}],
                )
                count += 1
    _log_action(request, 'Wishlist trigger', before='check', after=f'{count} tasks')
    messages.success(request, f'Создано {count} автозадач по триггеру wishlist.')
    return redirect(f"{reverse('crm:dashboard')}?page=tasks")


def _create_order(request):
    client = Client.objects.filter(pk=request.POST.get('client_id')).first()
    if not client:
        messages.error(request, 'Выберите клиента.')
        return redirect(f"{reverse('crm:dashboard')}?page=clients")
    items_raw = request.POST.get('items', '')
    items = []
    total = 0
    for line in items_raw.split('\n'):
        line = line.strip()
        if not line:
            continue
        parts = line.split(',')
        if len(parts) >= 3:
            name = parts[0].strip()
            qty = int(parts[1].strip())
            price = float(parts[2].strip())
            items.append({'name': name, 'qty': qty, 'price': price, 'sku': parts[3].strip() if len(parts) > 3 else ''})
            total += qty * price
    notes = request.POST.get('notes', '').strip()
    order = Order.objects.create(client=client, items=items, total=total, notes=notes, history=[])
    client.history = (client.history or []) + [{'type': 'order', 'text': f'Создан заказ №{order.pk} на сумму {_fmt_money(total)}', 'at': timezone.now().isoformat()}]
    client.save(update_fields=['history', 'updated_at'])
    _log_action(request, 'Create order', before='new', after=f'order #{order.pk}')
    messages.success(request, f'Заказ №{order.pk} создан.')
    return redirect(f"{reverse('crm:dashboard')}?page=clients&client={client.pk}")


def _update_order_status(request):
    order = Order.objects.filter(pk=request.POST.get('order_id')).first()
    if order:
        new_status = request.POST.get('status', '')
        if new_status in dict(Order.Status.choices):
            old_status = order.status
            order.status = new_status
            order.history = (order.history or []) + [{'from': old_status, 'to': new_status, 'at': timezone.now().isoformat()}]
            order.save()
            _log_action(request, 'Update order status', before=old_status, after=new_status)
            messages.success(request, f'Статус заказа №{order.pk} изменён.')
    return redirect(f"{reverse('crm:dashboard')}?page=clients")


def _edit_order(request):
    order = Order.objects.filter(pk=request.POST.get('order_id')).first()
    if order:
        items_raw = request.POST.get('items', '')
        items = []
        total = 0
        for line in items_raw.split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = line.split(',')
            if len(parts) >= 3:
                name = parts[0].strip()
                qty = int(parts[1].strip())
                price = float(parts[2].strip())
                items.append({'name': name, 'qty': qty, 'price': price, 'sku': parts[3].strip() if len(parts) > 3 else ''})
                total += qty * price
        notes = request.POST.get('notes', '').strip()
        order.items = items
        order.total = total
        order.notes = notes
        order.history = (order.history or []) + [{'action': 'edit', 'at': timezone.now().isoformat()}]
        order.save()
        _log_action(request, 'Edit order', before=f'order #{order.pk}', after='edited')
        messages.success(request, f'Заказ №{order.pk} обновлён.')
    return redirect(f"{reverse('crm:dashboard')}?page=clients")


def _delete_order(request):
    order = Order.objects.filter(pk=request.POST.get('order_id')).first()
    if order:
        client = order.client
        pk = order.pk
        order.delete()
        if client:
            client.history = (client.history or []) + [{'type': 'order', 'text': f'Заказ №{pk} удалён', 'at': timezone.now().isoformat()}]
            client.save(update_fields=['history', 'updated_at'])
        _log_action(request, 'Delete order', before=f'order #{pk}', after='deleted')
        messages.success(request, f'Заказ №{pk} удалён.')
    return redirect(f"{reverse('crm:dashboard')}?page=clients")


def _save_article(request):
    title = request.POST.get('article_title', '').strip()
    role = request.POST.get('article_role', '').strip()
    body = request.POST.get('article_body', '').strip()
    article_id = request.POST.get('article_id', '').strip()
    if not title or not body:
        messages.error(request, 'Заполните название и текст статьи.')
        return redirect(f"{reverse('crm:dashboard')}?page=admin")
    article = KnowledgeArticle.objects.filter(pk=article_id).first() if article_id else None
    if article:
        article.title = title
        article.role = role
        article.body = body
        article.save()
        _log_action(request, 'Update article', before=str(article.id), after=title)
        messages.success(request, f'Статья «{title}» обновлена.')
    else:
        KnowledgeArticle.objects.create(title=title, role=role, body=body)
        _log_action(request, 'Create article', before='new', after=title)
        messages.success(request, f'Статья «{title}» создана.')
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _delete_article(request):
    article = KnowledgeArticle.objects.filter(pk=request.POST.get('article_id')).first()
    if article:
        _log_action(request, 'Delete article', before=article.title, after='removed')
        article.delete()
        messages.success(request, 'Статья удалена.')
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _quick_test_reset(request):
    request.session.pop('quick_test_question', None)
    request.session.pop('quick_test_answer', None)
    request.session.pop('quick_test_accept', None)
    _log_action(request, 'Quick test reset', before='active', after='cancelled')
    messages.success(request, 'Тест сброшен.')
    return redirect(f"{reverse('crm:dashboard')}?page=knowledge")


def _clock_toggle(request):
    event = ClockEvent.objects.filter(user=request.user, clock_out__isnull=True).first()
    if event:
        event.clock_out = timezone.now()
        event.save(update_fields=['clock_out'])
        _log_action(request, 'Clock out', before=event.clock_in.isoformat(), after='clock_out')
        messages.success(request, 'Рабочий день завершён.')
    else:
        ClockEvent.objects.create(user=request.user, clock_in=timezone.now())
        _log_action(request, 'Clock in', before='', after=timezone.now().isoformat())
        messages.success(request, 'Рабочий день начат.')
    return redirect(f"{reverse('crm:dashboard')}?page=dashboard")


def _reassign_task(request):
    task = Task.objects.filter(pk=request.POST.get('task_id')).first()
    if task:
        new_user = User.objects.filter(pk=request.POST.get('assigned_to')).first()
        if new_user:
            old = task.assigned_to.username if task.assigned_to else 'none'
            task.assigned_to = new_user
            task.comments = (task.comments or []) + [{'author': request.user.username, 'text': f'Переназначено на {new_user.username}', 'at': timezone.now().isoformat()}]
            task.save(update_fields=['assigned_to', 'comments'])
            _log_action(request, 'Reassign task', before=old, after=new_user.username)
            messages.success(request, f'Задача переназначена на {new_user.username}.')
    return redirect(f"{reverse('crm:dashboard')}?page=tasks")


def _export_clients_csv(request):
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="clients.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', 'Имя', 'Телефон', 'Email', 'ID 1С', 'Источник', 'Статус', 'Теги', 'Интересы', 'Покупки'])
    for client in Client.objects.all():
        writer.writerow([client.id, client.name, client.phone or '', client.email or '', client.one_c_id or '',
                         client.source or '', client.get_status_display(), ', '.join(client.tags or []),
                         ', '.join(client.interests or []), client.purchase_count])
    _log_action(request, 'Export clients CSV', before='', after=f'{Client.objects.count()} rows')
    return response


def _export_products_csv(request):
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="products.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', 'Название', 'Родитель', 'SKU', 'Тип', 'Остаток', 'Резерв', 'Цена', 'В производстве', 'Статус'])
    for product in Product.objects.all():
        writer.writerow([product.id, product.name, product.parent, product.sku, product.get_kind_display(),
                         product.stock, product.reserve, product.price, product.in_production, product.get_status_display()])
    _log_action(request, 'Export products CSV', before='', after=f'{Product.objects.count()} rows')
    return response


def _save_script(request):
    script_id = request.POST.get('script_id', '').strip()
    trigger = request.POST.get('trigger', '').strip()
    answer = request.POST.get('answer', '').strip()
    if not trigger or not answer:
        messages.error(request, 'Заполните триггер и ответ.')
        return redirect(f"{reverse('crm:dashboard')}?page=admin")
    if script_id:
        script = ScriptRule.objects.filter(pk=script_id).first()
        if script:
            script.trigger = trigger
            script.answer = answer
            script.save()
            _log_action(request, 'Update script', before=script.trigger, after=trigger)
            messages.success(request, f'Скрипт «{trigger}» обновлён.')
    else:
        ScriptRule.objects.create(trigger=trigger, answer=answer)
        _log_action(request, 'Create script', before='new', after=trigger)
        messages.success(request, f'Скрипт «{trigger}» создан.')
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _delete_script(request):
    script = ScriptRule.objects.filter(pk=request.POST.get('script_id')).first()
    if script:
        _log_action(request, 'Delete script', before=script.trigger, after='removed')
        script.delete()
        messages.success(request, 'Скрипт удалён.')
    return redirect(f"{reverse('crm:dashboard')}?page=admin")


def _redirect_to_client(request, client_id):
    page = request.GET.get('page', 'clients')
    return f"{reverse('crm:dashboard')}?page={page}&client={client_id or ''}"


@login_required
def client_detail_json(request, client_id):
    client = get_object_or_404(Client.objects.prefetch_related('messages', 'tasks', 'orders'), pk=client_id)
    messages_list = []
    for m in client.messages.all()[:20]:
        messages_list.append({
            'id': m.id,
            'channel': m.channel,
            'direction': m.get_direction_display(),
            'text': m.text,
            'author': m.author_name,
            'created_at': _fmt_dt(m.created_at),
        })
    tasks_list = []
    for t in client.tasks.all()[:10]:
        tasks_list.append({
            'id': t.id,
            'title': t.title,
            'status': t.get_status_display(),
            'priority': t.priority,
            'due_at': _fmt_dt(t.due_at),
        })
    orders_list = []
    for o in client.orders.all()[:10]:
        orders_list.append({
            'id': o.id,
            'status': o.get_status_display(),
            'total': str(o.total),
            'notes': o.notes,
            'items': o.items,
            'created_at': _fmt_dt(o.created_at),
        })
    return JsonResponse({
        'id': client.id,
        'name': client.name,
        'phone': client.phone or '',
        'email': client.email or '',
        'source': client.source or '',
        'status_label': client.status_label,
        'status_class': _status_class(client.status),
        'tags': client.tags,
        'interests': client.interests,
        'wish_list': client.wish_list,
        'internal_note': client.internal_note or '',
        'quality': client.quality,
        'green_list': client.green_list,
        'black_list': client.black_list,
        'purchase_count': client.purchase_count,
        'messages': messages_list,
        'tasks': tasks_list,
        'orders': orders_list,
        'history': client.history[-10:] if client.history else [],
    })
