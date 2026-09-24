import asyncio
import json
import os

from django.core.management.base import BaseCommand

from apps.core.models import Organization, User
from apps.marvins.models import Capability, Marvin
from apps.sessions.models import Thread, TSession
from services.temporal_workers.client import get_temporal_client
from services.temporal_workers.workflows import CapabilityExecutionWorkflow


class Command(BaseCommand):
    help = "Execute a capability on a Marvin agent via Temporal workflow"

    def add_arguments(self, parser):
        parser.add_argument("org_slug", help="Organization slug")
        parser.add_argument("capability_name", help="Capability name to execute")
        parser.add_argument("--labels", default=None, help="Label selector, e.g. 'env:production'")
        parser.add_argument(
            "--parameters",
            default="{}",
            help='JSON parameters dict, e.g. \'{"host":"1.2.3.4"}\'',
        )
        parser.add_argument("--session-id", default=None, help="Existing session ID")
        parser.add_argument("--thread-id", default=None, help="Existing thread ID")
        parser.add_argument("--marvin-id", default=None, help="Specific Marvin ID")
        parser.add_argument(
            "--timeout", type=int, default=60, help="Workflow timeout in seconds (default: 60)"
        )

    async def _dispatch_workflow(
        self, session_id, thread_id, capability_name, parameters, marvin_id, timeout
    ):
        temporal_client = await get_temporal_client()
        return await asyncio.wait_for(
            temporal_client.execute_workflow(
                CapabilityExecutionWorkflow.run,
                args=[
                    str(session_id),
                    str(thread_id),
                    capability_name,
                    parameters,
                    str(marvin_id),
                ],
                id=f"test-capability-{capability_name}-{thread_id}",
                task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "vogon"),
            ),
            timeout=timeout,
        )

    def handle(self, *args, **options):
        org_slug = options["org_slug"]
        capability_name = options["capability_name"]
        labels = options["labels"]
        parameters_json = options["parameters"]
        session_id = options["session_id"]
        thread_id = options["thread_id"]
        marvin_id = options["marvin_id"]
        timeout = options["timeout"]

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"Organization not found: {org_slug}"))
            raise SystemExit(1)

        try:
            capability = Capability.objects.get(
                organization=org, name=capability_name, enabled=True
            )
        except Capability.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(f"Capability not found or disabled: {capability_name}")
            )
            raise SystemExit(1)

        marvin = None
        if marvin_id:
            try:
                marvin = Marvin.objects.get(id=marvin_id, organization=org)
            except Marvin.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"Marvin not found: {marvin_id}"))
                raise SystemExit(1)
        else:
            qs = capability.marvins.filter(organization=org, status=Marvin.Status.ONLINE)
            if labels:
                from apps.marvins.labels import marvin_matches_labels, parse_label_selector

                selector = parse_label_selector(labels)
                for m in qs:
                    if marvin_matches_labels(m, selector):
                        marvin = m
                        break
            else:
                marvin = qs.first()

        if not marvin:
            self.stderr.write(
                self.style.ERROR(
                    "No online Marvin found with this capability (and matching labels)"
                )
            )
            raise SystemExit(1)

        self.stdout.write(
            f"Selected Marvin: {marvin.name} ({marvin.client_id}) status={marvin.status}"
        )

        if session_id:
            session = TSession.objects.get(id=session_id)
        else:
            session = TSession.objects.create(organization=org, title=f"Test {capability_name}")
            self.stdout.write(f"Created session: {session.id}")

        if thread_id:
            thread = Thread.objects.get(id=thread_id, tsession=session)
        else:
            user = User.objects.filter(organizations=org).first()
            if not user:
                user = User.objects.create_user(username="test-runner", password="test")
                org.users.add(user)
            thread, created = Thread.objects.get_or_create(
                tsession=session, user=user, defaults={"title": f"Test {capability_name}"}
            )
            if created:
                self.stdout.write(f"Created thread: {thread.id}")
            else:
                self.stdout.write(f"Reusing existing thread: {thread.id}")

        parameters = json.loads(parameters_json)

        self.stdout.write("")
        self.stdout.write("Dispatching CapabilityExecutionWorkflow...")
        self.stdout.write(f"  session_id={session.id}")
        self.stdout.write(f"  thread_id={thread.id}")
        self.stdout.write(f"  capability_name={capability_name}")
        self.stdout.write(f"  marvin_id={marvin.id} ({marvin.name})")
        self.stdout.write(f"  parameters={parameters}")

        try:
            result = asyncio.run(
                self._dispatch_workflow(
                    session.id,
                    thread.id,
                    capability_name,
                    parameters,
                    marvin.id,
                    timeout,
                )
            )
            self.stdout.write("")
            self.stdout.write(f"Result: {json.dumps(result, indent=2)}")
        except TimeoutError:
            self.stderr.write(self.style.ERROR("Workflow timed out"))
            raise SystemExit(1)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error: {e}"))
            raise SystemExit(1)
