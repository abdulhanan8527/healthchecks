from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from datetime import timedelta as td

from django import forms

from hc.api import rules as rules_lib
from hc.api.models import Check
from hc.front.validators import (
    CronValidator,
    OnCalendarValidator,
    TimezoneValidator,
    WebhookValidator,
)


def _choices(csv: str) -> list[tuple[str, str]]:
    return [(v, v) for v in csv.split(",")]


class LaxURLField(forms.URLField):
    """Subclass of URLField which additionally accepts URLs without a tld.

    For example, unlike URLField, it accepts "http://home_server"

    """

    default_validators = [WebhookValidator()]


class NameTagsForm(forms.Form):
    name = forms.CharField(max_length=100, required=False)
    slug = forms.SlugField(max_length=100, required=False)
    tags = forms.CharField(max_length=500, required=False)
    desc = forms.CharField(max_length=10000, required=False)

    def clean_tags(self) -> str:
        result = []

        for part in self.cleaned_data["tags"].split(" "):
            part = part.strip()
            if part != "":
                result.append(part)

        return " ".join(result)


class AddCheckForm(NameTagsForm):
    kind = forms.ChoiceField(choices=_choices("simple,cron,oncalendar"))
    timeout = forms.IntegerField(min_value=60, max_value=31536000)
    schedule = forms.CharField(required=False, max_length=100)
    tz = forms.CharField(max_length=36, validators=[TimezoneValidator()])
    grace = forms.IntegerField(min_value=60, max_value=31536000)
    api_url = LaxURLField(required=False, max_length=1000, assume_scheme="https")
    api_method = forms.ChoiceField(
        required=False, choices=(("GET", "GET"), ("POST", "POST")), initial="GET"
    )
    api_token = forms.CharField(required=False, max_length=500)
    api_rules = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Optional JSON list of success rules.",
    )

    def clean_api_rules(self) -> str:
        text = self.cleaned_data["api_rules"]
        try:
            rules_lib.parse_rules(text)
        except rules_lib.RulesError as e:
            raise forms.ValidationError(str(e))

        return text

    api_skip_paths = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Optional JSON paths to omit from the email body, one per line.",
    )

    def clean_timeout(self) -> td:
        return td(seconds=self.cleaned_data["timeout"])

    def clean_grace(self) -> td:
        return td(seconds=self.cleaned_data["grace"])

    def clean_schedule(self) -> str:
        kind = self.cleaned_data.get("kind")
        if kind == "cron":
            cron_validator = CronValidator()
            cron_validator(self.cleaned_data["schedule"])
        elif kind == "oncalendar":
            oncalendar_validator = OnCalendarValidator()
            oncalendar_validator(self.cleaned_data["schedule"])
        else:
            # If kind is not cron or oncalendar, ignore the passed in value
            # and use "* * * * *" instead.
            return "* * * * *"

        assert isinstance(self.cleaned_data["schedule"], str)
        return self.cleaned_data["schedule"]


class ProbeSettingsForm(forms.ModelForm):
    """Edit the active-probing settings of an existing check."""

    api_rules = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 6, "class": "form-control"}),
    )
    api_method = forms.ChoiceField(
        choices=(("GET", "GET"), ("POST", "POST")),
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    probe_min_failures = forms.IntegerField(
        min_value=1,
        max_value=10,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    probe_escalate_after = forms.IntegerField(
        min_value=0,
        max_value=43200,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    depends_on = forms.ModelMultipleChoiceField(
        queryset=Check.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    api_timeout = forms.IntegerField(
        min_value=0,
        max_value=600,
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    probe_remote = forms.BooleanField(required=False, widget=forms.CheckboxInput())

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        instance = self.instance
        if instance is not None and getattr(instance, "project_id", None):
            self.fields["depends_on"].queryset = (
                Check.objects.filter(project=instance.project)
                .exclude(id=instance.id)
                .order_by("created")
            )

    class Meta:
        model = Check
        fields = [
            "api_url",
            "api_method",
            "api_token",
            "api_headers",
            "api_body",
            "api_timeout",
            "api_rules",
            "api_skip_paths",
            "probe_min_failures",
            "probe_escalate_after",
            "probe_remote",
            "depends_on",
        ]
        widgets = {
            "api_url": forms.TextInput(attrs={"class": "form-control"}),
            "api_token": forms.TextInput(attrs={"class": "form-control"}),
            "api_headers": forms.Textarea(
                attrs={"rows": 3, "class": "form-control"}
            ),
            "api_body": forms.Textarea(attrs={"rows": 4, "class": "form-control"}),
            "api_skip_paths": forms.Textarea(
                attrs={"rows": 2, "class": "form-control"}
            ),
        }

    def clean_api_rules(self) -> str:
        text = self.cleaned_data["api_rules"]
        try:
            rules_lib.parse_rules(text)
        except rules_lib.RulesError as e:
            raise forms.ValidationError(str(e))

        return text

    def clean_api_headers(self) -> str:
        text = self.cleaned_data["api_headers"]
        if not text.strip():
            return ""

        try:
            data = json.loads(text)
        except ValueError as e:
            raise forms.ValidationError(f"Not valid JSON: {e}")

        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise forms.ValidationError(
                "Must be a JSON object with string keys and values."
            )

        return text

    def clean_api_timeout(self) -> int:
        # Blank means "use the global default" -> store 0:
        return self.cleaned_data.get("api_timeout") or 0


class FilteringRulesForm(forms.Form):
    filter_subject = forms.BooleanField(required=False)
    filter_body = forms.BooleanField(required=False)
    filter_http_body = forms.BooleanField(required=False)
    filter_default_fail = forms.BooleanField(required=False)
    start_kw = forms.CharField(required=False, max_length=200)
    success_kw = forms.CharField(required=False, max_length=200)
    failure_kw = forms.CharField(required=False, max_length=200)
    methods = forms.ChoiceField(required=False, choices=(("", "Any"), ("POST", "POST")))
    manual_resume = forms.BooleanField(required=False)


class TimeoutForm(forms.Form):
    timeout = forms.IntegerField(min_value=60, max_value=31536000)
    grace = forms.IntegerField(min_value=60, max_value=31536000)

    def clean_timeout(self) -> td:
        return td(seconds=self.cleaned_data["timeout"])

    def clean_grace(self) -> td:
        return td(seconds=self.cleaned_data["grace"])


class CronPreviewForm(forms.Form):
    schedule = forms.CharField(max_length=100, validators=[CronValidator()])
    tz = forms.CharField(max_length=36, validators=[TimezoneValidator()])


class CronForm(forms.Form):
    schedule = forms.CharField(max_length=100, validators=[CronValidator()])
    tz = forms.CharField(max_length=36, validators=[TimezoneValidator()])
    grace = forms.IntegerField(min_value=60, max_value=31536000)

    def clean_grace(self) -> td:
        return td(seconds=self.cleaned_data["grace"])


class OnCalendarForm(forms.Form):
    schedule = forms.CharField(max_length=100, validators=[OnCalendarValidator()])
    tz = forms.CharField(max_length=36, validators=[TimezoneValidator()])
    grace = forms.IntegerField(min_value=60, max_value=31536000)

    def clean_grace(self) -> td:
        return td(seconds=self.cleaned_data["grace"])


class AddUrlForm(forms.Form):
    error_css_class = "has-error"
    value = LaxURLField(max_length=1000, assume_scheme="https")


class PhoneNumberForm(forms.Form):
    error_css_class = "has-error"
    label = forms.CharField(max_length=100, required=False)
    phone = forms.CharField()

    def clean_phone(self) -> str:
        v = self.cleaned_data["phone"]
        assert isinstance(v, str)

        stripped = v.encode("ascii", "ignore").decode("ascii")
        stripped = stripped.replace(" ", "").replace("-", "")
        if not re.match(r"^\+\d{5,15}$", stripped):
            raise forms.ValidationError("Invalid phone number format.")

        return stripped

    def get_json(self) -> str:
        return json.dumps({"value": self.cleaned_data["phone"]})


class PhoneUpDownForm(PhoneNumberForm):
    up = forms.BooleanField(required=False, initial=True)
    down = forms.BooleanField(required=False, initial=True)

    def clean(self) -> None:
        super().clean()

        down = self.cleaned_data.get("down")
        up = self.cleaned_data.get("up")

        if not down and not up:
            self.add_error("down", "Please select at least one.")

    def get_json(self) -> str:
        return json.dumps(
            {
                "value": self.cleaned_data["phone"],
                "up": self.cleaned_data["up"],
                "down": self.cleaned_data["down"],
            }
        )


class ChannelNameForm(forms.Form):
    name = forms.CharField(max_length=100, required=False)


class SearchForm(forms.Form):
    q = forms.RegexField(regex=r"^[0-9a-zA-Z\s]{3,100}$")


class LogFiltersForm(forms.Form):
    # min_value is 2010-01-01, max_value is 2030-01-01
    u = forms.FloatField(min_value=1262296800, max_value=1893448800, required=False)
    end = forms.FloatField(min_value=1262296800, max_value=1893448800, required=False)
    success = forms.BooleanField(required=False)
    fail = forms.BooleanField(required=False)
    start = forms.BooleanField(required=False)
    log = forms.BooleanField(required=False)
    ign = forms.BooleanField(required=False)
    notification = forms.BooleanField(required=False)
    flip = forms.BooleanField(required=False)

    def clean_u(self) -> datetime | None:
        if self.cleaned_data["u"]:
            return datetime.fromtimestamp(self.cleaned_data["u"], tz=timezone.utc)
        return None

    def clean_end(self) -> datetime | None:
        if self.cleaned_data["end"]:
            return datetime.fromtimestamp(self.cleaned_data["end"], tz=timezone.utc)
        return None

    def kinds(self) -> tuple[str, ...]:
        kind_keys = ("success", "fail", "start", "log", "ign", "notification", "flip")
        return tuple(key for key in kind_keys if self.cleaned_data[key])


class TransferForm(forms.Form):
    project = forms.UUIDField()


class BadgeSettingsForm(forms.Form):
    target = forms.ChoiceField(choices=_choices("all,tag,check"))
    tag = forms.CharField(max_length=100, required=False)
    check = forms.UUIDField(required=False)
    fmt = forms.ChoiceField(choices=_choices("svg,json,shields"))
    states = forms.ChoiceField(choices=_choices("2,3"))
