""" WSGI for the scheduler app. """

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "task_scheduler.settings")

application = get_wsgi_application()

