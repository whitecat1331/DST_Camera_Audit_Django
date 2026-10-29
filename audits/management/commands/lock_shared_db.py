import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

logger = logging.getLogger(__name__)

# Supabase exposes `public` to these roles through its Data API (PostgREST).
API_ROLES = "anon, authenticated"

CHECK_SQL = """
select count(*) filter (where has_table_privilege('anon', c.oid, 'SELECT')),
       count(*) filter (where has_table_privilege('authenticated', c.oid, 'SELECT')),
       count(*) filter (where not c.relrowsecurity),
       count(*)
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r'
"""

LOCK_SQL = f"""
do $$
declare t record;
begin
  revoke all on all tables    in schema public from {API_ROLES};
  revoke all on all sequences in schema public from {API_ROLES};
  revoke all on all functions in schema public from {API_ROLES};
  alter default privileges for role current_user in schema public revoke all on tables    from {API_ROLES};
  alter default privileges for role current_user in schema public revoke all on sequences from {API_ROLES};
  alter default privileges for role current_user in schema public revoke all on functions from {API_ROLES};
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I enable row level security', t.tablename);
  end loop;
end $$;
"""


class Command(BaseCommand):
    help = (
        "Lock the shared Supabase DB against its Data API: revoke anon/authenticated "
        "and enable RLS on every public table. Run after every migrate on it."
    )

    def add_arguments(self, parser):
        default = "default" if settings.DST_ROLE == "review" else "shared"
        parser.add_argument("--database", default=default)
        parser.add_argument(
            "--check", action="store_true", help="Report only; exit 1 if anything is exposed."
        )

    def handle(self, *args, **options):
        alias = options["database"]
        if alias not in connections.databases:
            raise CommandError(f"No database alias '{alias}' (is DB_HOST set?)")
        conn = connections[alias]
        if conn.vendor != "postgresql":
            raise CommandError(f"'{alias}' is {conn.vendor}, not the shared Postgres DB")
        with conn.cursor() as cur:
            cur.execute(CHECK_SQL)
            before = cur.fetchone()
            self.stdout.write(self._fmt("before", before))
            if options["check"]:
                if self._exposed(before):
                    raise CommandError("Shared DB is exposed to the Supabase API roles")
                return
            cur.execute(LOCK_SQL)
            cur.execute(CHECK_SQL)
            after = cur.fetchone()
        self.stdout.write(self._fmt("after", after))
        if self._exposed(after):
            raise CommandError("Lockdown incomplete")
        logger.info("[INIT] lock_shared_db %s: %s tables locked", alias, after[3])
        self.stdout.write(self.style.SUCCESS(f"Locked {after[3]} tables on '{alias}'"))

    @staticmethod
    def _exposed(row) -> bool:
        anon, auth, rls_off, _ = row
        return bool(anon or auth or rls_off)

    @staticmethod
    def _fmt(tag, row) -> str:
        anon, auth, rls_off, total = row
        return f"{tag}: anon_select={anon} authenticated_select={auth} rls_off={rls_off} tables={total}"
