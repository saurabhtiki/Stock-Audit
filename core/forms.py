from django import forms

from core.models import Branch, Company, Department


class StyledFormMixin:
    """Adds daisyUI classes to every widget so templates stay simple."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                css = "toggle toggle-primary"
            elif isinstance(widget, forms.ClearableFileInput | forms.FileInput):
                css = "file-input w-full"
            elif isinstance(widget, forms.Select):
                css = "select w-full"
            elif isinstance(widget, forms.Textarea):
                css = "textarea w-full"
                widget.attrs.setdefault("rows", 3)
            else:
                css = "input w-full"
            widget.attrs["class"] = f"{css} {widget.attrs.get('class', '')}".strip()
            if field.help_text:
                widget.attrs.setdefault("title", field.help_text)


class CompanyForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Company
        fields = ["name", "address", "logo"]
        help_texts = {"name": "Full company name, as on documents."}


class BranchForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Branch
        fields = ["company", "name", "code"]
        help_texts = {"company": "Company this Branch belongs to. Cannot be changed later."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["company"].queryset = Company.objects.filter(is_active=True)
        if self.instance.pk:
            self.fields["company"].disabled = True

    def clean_code(self):
        return self.cleaned_data["code"].strip().upper()

    def clean(self):
        cleaned = super().clean()
        company = cleaned.get("company") or getattr(self.instance, "company", None)
        code = cleaned.get("code")
        if company and code:
            clash = Branch.objects.filter(company=company, code=code).exclude(pk=self.instance.pk)
            if clash.exists():
                self.add_error(
                    "code", "This code is already used by another Branch of this company."
                )
        return cleaned


class DepartmentForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Department
        fields = ["company", "name"]
        help_texts = {"company": "Company this department belongs to. Cannot be changed later."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["company"].queryset = Company.objects.filter(is_active=True)
        if self.instance.pk:
            self.fields["company"].disabled = True

    def clean_name(self):
        return " ".join(self.cleaned_data["name"].split())

    def clean(self):
        cleaned = super().clean()
        company = cleaned.get("company") or getattr(self.instance, "company", None)
        name = cleaned.get("name")
        if company and name:
            clash = Department.objects.filter(company=company, name__iexact=name)
            if clash.exclude(pk=self.instance.pk).exists():
                self.add_error("name", "This company already has a department with this name.")
        return cleaned
