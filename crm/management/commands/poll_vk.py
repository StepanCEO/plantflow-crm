from django.core.management.base import BaseCommand

from crm.vk_integration import poll_vk_messages


class Command(BaseCommand):
    help = 'Poll VK messages and import them into CRM'

    def handle(self, *args, **options):
        self.stdout.write('Polling VK messages...')
        result = poll_vk_messages()
        status = result.get('status', 'error')
        message = result.get('message', 'Unknown error')
        imported = result.get('imported', 0)

        if status == 'ok':
            self.stdout.write(self.style.SUCCESS(f'OK — {message}'))
        else:
            self.stdout.write(self.style.ERROR(f'Error — {message}'))

        if imported > 0:
            self.stdout.write(self.style.SUCCESS(f'Imported {imported} messages from VK'))
