from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from crm.models import EmployeeProfile


class Command(BaseCommand):
    help = 'Set email addresses for CRM users'

    def add_arguments(self, parser):
        parser.add_argument('--admin-email', default='', help='Email for admin user')

    def handle(self, *args, **options):
        updates = {
            'admin': options.get('admin_email') or 'secretgarden-kassa@yandex.ru',
        }
        for username, email in updates.items():
            user = User.objects.filter(username=username).first()
            if user:
                user.email = email
                user.save(update_fields=['email'])
                self.stdout.write(self.style.SUCCESS(f'{username}: email → {email}'))
            else:
                self.stdout.write(self.style.WARNING(f'{username}: not found'))
