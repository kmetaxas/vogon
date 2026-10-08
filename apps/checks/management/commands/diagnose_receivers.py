"""Management command to diagnose CheckReceiver LLM provider configuration."""

from django.core.management.base import BaseCommand

from apps.checks.models import CheckReceiver
from apps.llm.models import LLMProvider


class Command(BaseCommand):
    help = "Diagnose CheckReceiver admission gate LLM provider configuration"

    def handle(self, *args, **options):
        receivers = CheckReceiver.objects.select_related(
            "check", "organization", "admission_llm_provider"
        ).all()

        if not receivers.exists():
            self.stdout.write(self.style.WARNING("No CheckReceivers found."))
            return

        issues = []
        self.stdout.write(self.style.NOTICE("=" * 70))
        self.stdout.write("CheckReceiver Admission Gate Diagnostics")
        self.stdout.write(self.style.NOTICE("=" * 70))
        self.stdout.write()

        for receiver in receivers:
            provider = receiver.admission_llm_provider
            mode = receiver.admission_mode

            self.stdout.write(f"Receiver: {receiver.name} ({receiver.id})")
            self.stdout.write(f"  Check: {receiver.check.name}")
            self.stdout.write(f"  Org: {receiver.organization.name}")
            self.stdout.write(f"  Admission Mode: {mode}")

            if provider:
                self.stdout.write(f"  Provider: {provider.name} ({provider.model})")
                self.stdout.write(f"  Provider is_jev: {'YES' if provider.is_jev else 'NO'}")

                if mode == "ai_gated" and not provider.is_jev:
                    issues.append(
                        {
                            "receiver": receiver,
                            "problem": "AI_GATED mode uses non-JEV provider",
                            "fix": (
                                "Select a JEV provider (is_jev=True) for receiver "
                                f"'{receiver.name}'"
                            ),
                        }
                    )
                    self.stdout.write(
                        self.style.ERROR("  ❌ PROBLEM: AI_GATED mode but provider is NOT JEV!")
                    )
                    self.stdout.write(
                        self.style.ERROR(
                            "     This causes JSON parse failures because non-JEV models"
                        )
                    )
                    self.stdout.write(
                        self.style.ERROR(
                            "     (like DeepSeek) don't understand the admission gate format."
                        )
                    )
                elif mode == "ai_gated" and provider.is_jev:
                    self.stdout.write(self.style.SUCCESS("  ✓ Correct: JEV provider for AI_GATED"))
                elif mode == "always":
                    self.stdout.write(self.style.NOTICE("  ℹ ALWAYS mode — LLM not used"))
            else:
                self.stdout.write("  Provider: None (not set)")
                if mode == "ai_gated":
                    issues.append(
                        {
                            "receiver": receiver,
                            "problem": (
                                "AI_GATED mode with no explicit provider — will use org JEV default"
                            ),
                            "fix": f"Set a JEV provider on receiver '{receiver.name}'",
                        }
                    )
                    self.stdout.write(
                        self.style.WARNING(
                            "  ⚠ AI_GATED mode with no explicit provider — will use org JEV default"
                        )
                    )

            self.stdout.write()

        # Show available JEV providers
        jev_providers = LLMProvider.objects.filter(is_jev=True, enabled=True)
        self.stdout.write(self.style.NOTICE("=" * 70))
        self.stdout.write("Available JEV Providers")
        self.stdout.write(self.style.NOTICE("=" * 70))
        if jev_providers.exists():
            for p in jev_providers:
                self.stdout.write(f"  - {p.name} (model={p.model}, id={p.id})")
        else:
            self.stdout.write(
                self.style.WARNING("  No JEV providers found! Create one with is_jev=True.")
            )
        self.stdout.write()

        # Show all providers
        all_providers = LLMProvider.objects.filter(enabled=True)
        self.stdout.write(self.style.NOTICE("=" * 70))
        self.stdout.write("All Enabled Providers")
        self.stdout.write(self.style.NOTICE("=" * 70))
        for p in all_providers:
            jev_badge = " [JEV]" if p.is_jev else ""
            default_badge = " [DEFAULT]" if p.is_default else ""
            self.stdout.write(f"  - {p.name} (model={p.model}){jev_badge}{default_badge}")
        self.stdout.write()

        # Summary
        self.stdout.write(self.style.NOTICE("=" * 70))
        if issues:
            self.stdout.write(self.style.ERROR(f"Found {len(issues)} configuration issue(s):"))
            for i, issue in enumerate(issues, 1):
                self.stdout.write(f"\n  {i}. {issue['problem']}")
                self.stdout.write(f"     Fix: {issue['fix']}")
                self.stdout.write(
                    f"     Action: Edit receiver at /checks/receivers/{issue['receiver'].id}/edit/"
                )
            self.stdout.write()
            self.stdout.write(self.style.WARNING("To fix via Django shell:"))
            self.stdout.write("  python manage.py shell")
            self.stdout.write("  >>> from apps.checks.models import CheckReceiver")
            self.stdout.write("  >>> from apps.llm.models import LLMProvider")
            self.stdout.write(
                '  >>> jev = LLMProvider.objects.get(is_jev=True, name="your-jev-name")'
            )
            self.stdout.write('  >>> r = CheckReceiver.objects.get(id="<receiver-id>")')
            self.stdout.write("  >>> r.admission_llm_provider = jev")
            self.stdout.write("  >>> r.save()")
        else:
            self.stdout.write(self.style.SUCCESS("No configuration issues found!"))
