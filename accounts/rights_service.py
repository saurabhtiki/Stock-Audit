"""Saving and copying per-Branch rights, with activity logging."""

from django.db import transaction

from accounts.models import BranchRight, Membership, User
from accounts.rights import RIGHT_NAMES, ROLE_DEFAULT_RIGHTS
from core.activity import log_activity
from core.models import Company


def rights_grid(user: User, membership: Membership) -> list[dict]:
    """One row per Branch: saved ticks, or the role's default ticks if never saved."""
    saved = {
        r.branch_id: r
        for r in BranchRight.objects.filter(user=user, branch__company=membership.company)
    }
    defaults = ROLE_DEFAULT_RIGHTS.get(membership.role, set())
    rows = []
    for branch in membership.company.branches.all():
        row = saved.get(branch.pk)
        rows.append(
            {
                "branch": branch,
                "ticked": row.granted() if row else set(defaults),
                "is_saved": row is not None,
            }
        )
    return rows


@transaction.atomic
def save_rights(actor: User, user: User, company: Company, ticks: dict[int, set[str]]) -> int:
    """Store the ticks for every Branch of the company. Returns how many Branches changed."""
    changed = 0
    existing = {
        r.branch_id: r for r in BranchRight.objects.filter(user=user, branch__company=company)
    }
    for branch in company.branches.all():
        wanted = ticks.get(branch.pk, set())
        row = existing.get(branch.pk)
        if row is None:
            # Save an explicit row (even if empty) so the grid stops showing role defaults.
            row = BranchRight(user=user, branch=branch)
            before = set()
        else:
            before = row.granted()
            if before == wanted:
                continue
        for name in RIGHT_NAMES:
            setattr(row, name, name in wanted)
        row.save()
        changes = {
            name: [name in before, name in wanted]
            for name in RIGHT_NAMES
            if (name in before) != (name in wanted)
        }
        log_activity(actor, "rights", row, changes)
        changed += 1
    return changed


def copy_rights(actor: User, source: User, target: User, company: Company) -> int:
    """Give target exactly the same Branch rights as source, in this company only."""
    source_rows = {
        r.branch_id: r.granted()
        for r in BranchRight.objects.filter(user=source, branch__company=company)
    }
    return save_rights(actor, target, company, source_rows)
