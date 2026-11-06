"""Start the app with Waitress, the same way on Windows and Linux.

    uv run python manage.py serve                       # http://127.0.0.1:8000
    uv run python manage.py serve --host 0.0.0.0        # reachable from other PCs (no HTTPS)

The environment comes from DJANGO_ENV as usual. On a real server, Caddy runs in front for
HTTPS and passes requests to this port (see docs/deploy.md).
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

# Headers Caddy adds so the app knows the visitor used HTTPS and who they are.
PROXY_HEADERS = {"x-forwarded-proto", "x-forwarded-for", "x-forwarded-host"}


class Command(BaseCommand):
    help = "Start the app with the Waitress web server (development, test or production)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--host",
            default="127.0.0.1",
            help="Address to listen on. 127.0.0.1 = this PC only (Caddy in front, the usual). "
            "0.0.0.0 = every network card.",
        )
        parser.add_argument("--port", type=int, default=8000, help="Port (default 8000).")
        parser.add_argument(
            "--threads", type=int, default=8, help="How many requests at once (default 8)."
        )
        parser.add_argument(
            "--trusted-proxy",
            default="127.0.0.1",
            help="Address of Caddy, whose HTTPS headers are believed (default 127.0.0.1). "
            "Use an empty value when nothing runs in front.",
        )

    def handle(self, *args, **options):
        if settings.DJANGO_ENV == "production":
            manifest = settings.STATIC_ROOT / "staticfiles.json"
            if not manifest.exists():
                logger.error("serve refused: %s is missing (collectstatic not run)", manifest)
                raise CommandError(
                    "CSS / JS files are not collected yet. Run "
                    "`uv run python manage.py collectstatic --no-input` first, then start again."
                )

        from waitress import serve

        from config.wsgi import application

        proxy = options["trusted_proxy"] or None
        logger.info(
            "Starting Stock Audit (%s) on http://%s:%s with %s threads",
            settings.DJANGO_ENV,
            options["host"],
            options["port"],
            options["threads"],
        )
        self.stdout.write(
            f"Stock Audit ({settings.DJANGO_ENV}) on http://{options['host']}:{options['port']} "
            "- press Ctrl+C to stop."
        )
        serve(
            application,
            host=options["host"],
            port=options["port"],
            threads=options["threads"],
            trusted_proxy=proxy,
            trusted_proxy_headers=PROXY_HEADERS if proxy else None,
            clear_untrusted_proxy_headers=True,
            ident="Stock Audit",
        )
