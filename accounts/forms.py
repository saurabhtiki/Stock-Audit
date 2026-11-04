import logging
from io import BytesIO

from django import forms
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.forms import PasswordChangeForm
from django.core.files.base import ContentFile
from django.core.validators import FileExtensionValidator
from PIL import Image, ImageOps, UnidentifiedImageError

from accounts.models import Membership, User, validate_photo_size
from accounts.rights import Role
from core.forms import StyledFormMixin
from core.models import Company

logger = logging.getLogger(__name__)


class LoginForm(StyledFormMixin, forms.Form):
    username = forms.CharField(max_length=150, widget=forms.TextInput(attrs={"autofocus": True}))
    password = forms.CharField(widget=forms.PasswordInput)

    def clean_username(self):
        return self.cleaned_data["username"].strip().lower()


class OwnPasswordChangeForm(StyledFormMixin, PasswordChangeForm):
    pass


class TemporaryPasswordMixin(forms.Form):
    temporary_password = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        help_text="At least 8 characters. The user must change it at first login.",
    )

    def clean_temporary_password(self):
        password = self.cleaned_data["temporary_password"]
        password_validation.validate_password(password)
        return password


class UserCreateForm(StyledFormMixin, TemporaryPasswordMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["username", "full_name", "email", "is_admin"]
        help_texts = {
            "username": "Used to log in. Unique across the whole group, e.g. ravi.k",
            "email": "Unique across the group. Stored only for now.",
        }

    def clean_username(self):
        username = self.cleaned_data["username"].strip().lower()
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("This username is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This email is already used by another user.")
        return email


class UserEditForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["full_name", "email", "is_admin"]

    clean_email = UserCreateForm.clean_email


class ProfilePhotoForm(StyledFormMixin, forms.ModelForm):
    """A user changes only their own photo. The photo is shrunk to a small JPEG."""

    class Meta:
        model = User
        fields = ["photo"]
        widgets = {"photo": forms.FileInput(attrs={"accept": "image/png,image/jpeg"})}

    def clean_photo(self):
        photo = self.cleaned_data["photo"]
        if "photo" not in self.changed_data:
            return photo
        # Check the file the user sent, before it is shrunk.
        FileExtensionValidator(["png", "jpg", "jpeg"])(photo)
        validate_photo_size(photo)
        try:
            image = ImageOps.exif_transpose(Image.open(photo)).convert("RGB")
        except (UnidentifiedImageError, OSError, ValueError) as error:
            logger.warning("Unreadable profile photo for %s: %s", self.instance.username, error)
            raise forms.ValidationError(
                "This file could not be read as a picture. Choose a PNG or JPG."
            ) from error
        side = settings.USER_PHOTO_MAX_PIXELS
        image.thumbnail((side, side))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        return ContentFile(buffer.getvalue(), name=f"{self.instance.username}.jpg")


class PasswordResetByAdminForm(StyledFormMixin, TemporaryPasswordMixin):
    pass


class MembershipAddForm(StyledFormMixin, forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.none())
    role = forms.ChoiceField(
        choices=Role.choices, help_text="Sets the starting ticks in the rights grid."
    )

    def __init__(self, *args, user: User, **kwargs):
        super().__init__(*args, **kwargs)
        taken = Membership.objects.filter(user=user).values("company_id")
        self.fields["company"].queryset = Company.objects.filter(is_active=True).exclude(
            pk__in=taken
        )
