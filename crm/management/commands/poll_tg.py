from django.core.management.base import BaseCommand

from crm.tg_integration import poll_tg_messages


class Command(BaseCommand):
    help = 'Poll Telegram bot updates and import messages into CRM'

    def handle(self, *args, **options):
        self.stdout.write('Polling Telegram messages...')
        result = poll_tg_messages()
        status = result.get('status', 'error')
        message = result.get('message', 'Unknown error')
        imported = result.get('imported', 0)

        if status == 'ok':
            self.stdout.write(self.style.SUCCESS(f'OK — {message}'))
        else:
            self.stdout.write(self.style.ERROR(f'Error — {message}'))

        if imported > 0:
            self.stdout.write(self.style.SUCCESS(f'Imported {imported} messages from Telegram'))
