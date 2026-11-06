from decimal import Decimal

from django import forms

from audits.models import Audit, Item
from core.forms import StyledFormMixin


class AuditForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Audit
        fields = ["branch", "title", "audit_date", "audit_type", "stock_as_on"]
        widgets = {
            "audit_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "stock_as_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }
        help_texts = {"branch": "Branch being audited. Cannot be changed later."}

    def __init__(self, *args, branches=None, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["branch"].disabled = True
        else:
            self.fields["branch"].queryset = self.fields["branch"].queryset.filter(
                pk__in=[b.pk for b in branches or []]
            )

    def clean_title(self):
        return " ".join(self.cleaned_data["title"].split())

    def clean(self):
        cleaned = super().clean()
        audit_date, stock_as_on = cleaned.get("audit_date"), cleaned.get("stock_as_on")
        if audit_date and stock_as_on and stock_as_on > audit_date:
            self.add_error("stock_as_on", "ERP stock date must be on or before the audit date.")
        branch = cleaned.get("branch") or getattr(self.instance, "branch", None)
        title = cleaned.get("title")
        if branch and title:
            clash = (
                Audit.objects.filter(branch=branch, title__iexact=title)
                .exclude(stage=Audit.Stage.CANCELLED)
                .exclude(pk=self.instance.pk)
            )
            if clash.exists():
                self.add_error("title", "This Branch already has an audit with this title.")
        return cleaned


class UploadForm(forms.Form):
    file = forms.FileField(
        label="ERP stock file",
        help_text="Excel (.xlsx) or CSV, made from the template. Up to 5 MB / 20,000 lines.",
        widget=forms.ClearableFileInput(
            attrs={"accept": ".xlsx,.csv", "class": "file-input w-full"}
        ),
    )


class FreezeForm(StyledFormMixin, forms.ModelForm):
    confirm = forms.BooleanField(
        label="I understand ERP lines and tolerance cannot be changed after this.",
        error_messages={"required": "Tick the box to confirm the freeze."},
    )

    class Meta:
        model = Audit
        fields = ["qty_tolerance", "pct_tolerance"]
        help_texts = {
            "qty_tolerance": "A line is flagged if its variance is more than this quantity. "
            "0 = any difference is flagged.",
            "pct_tolerance": "Optional. A line is flagged if its variance is more than this "
            "% of ERP Qty. Leave empty to not use it.",
        }

    def clean_qty_tolerance(self):
        value = self.cleaned_data["qty_tolerance"]
        if value is None or value < 0:
            raise forms.ValidationError("Enter 0 or more.")
        return value

    def clean_pct_tolerance(self):
        value = self.cleaned_data["pct_tolerance"]
        if value is not None and (value < 0 or value > Decimal(1000)):
            raise forms.ValidationError("Enter a % between 0 and 1000, or leave empty.")
        return value


class CancelForm(forms.Form):
    reason = forms.CharField(
        min_length=5,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3, "class": "textarea w-full"}),
        help_text="Why this audit is cancelled. Saved in the activity log.",
        error_messages={"required": "A reason is required to cancel an audit."},
    )


class ItemForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Item
        fields = ["code", "description", "uom"]
        help_texts = {"code": "Item code as in the ERP. Unique within the company."}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if self.instance.pk:
            # Codes link audit lines to items; changing one would break history.
            self.fields["code"].disabled = True

    def clean_code(self):
        return " ".join(self.cleaned_data["code"].split())

    def clean(self):
        cleaned = super().clean()
        code = cleaned.get("code")
        clash = Item.objects.filter(company=self.company, code__iexact=code or "")
        if code and not self.instance.pk and clash.exists():
            self.add_error("code", "This company already has an item with this code.")
        return cleaned


class RemarkForm(forms.Form):
    text = forms.CharField(
        max_length=1000,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "class": "textarea textarea-sm w-full",
                "placeholder": "Add a remark…",
            }
        ),
        error_messages={
            "required": "Type a remark first.",
            "max_length": "A remark can be at most 1000 characters.",
        },
    )


class ActionForm(forms.Form):
    """Only reads the due date safely; owner and text are checked in audits.queries."""

    due_date = forms.DateField(
        required=False,
        error_messages={"invalid": "Enter the due date as a date, e.g. 2026-09-15."},
    )


class CheckingDoneForm(forms.Form):
    confirm = forms.BooleanField(
        label="I understand counts cannot be changed after this, and queries will be raised.",
        error_messages={"required": "Tick the box to confirm Checking Done."},
    )
